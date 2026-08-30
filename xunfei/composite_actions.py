"""Composite-speaker editor actions for the Xunfei page.

The mixin owns editor selection, speaker marking, pause insertion, and the
composite preparation plan.  It shares the live page state with the browser
session but has no workflow or persistence responsibilities.
"""

from __future__ import annotations

import re
import time

from .config import (
    PARAM_DEFAULT,
    _MULTI_SELECT_MODIFIER,
    _SELECT_ALL,
    clamp_param,
)
from .errors import (
    XunfeiCancelled,
    XunfeiError,
    _check_cancel_requested,
    _log,
    _wait_with_cancel,
)
from .helpers import poll as _poll, safe_eval as _safe_eval
from .page_scripts import JS
from .voice_catalog import DEFAULT_FEMALE, get_voice_info


class CompositeActionsMixin:

    @staticmethod
    def _speaker_number(voice_key, info):
        """读取旧目录里的变体 ID；common/list 基础卡片可能没有该字段。

        多人配音选择是通过讯飞页面 UI 完成的。common/list 返回的基础
        音色卡片没有 speakerNo，页面点击卡片后会自行落到可用变体，因此
        缺少本地 speakerNo 不能再阻止合成；后续回读会确认页面生成了真实
        speaker mark。
        """
        value = info.get("speaker_no") or info.get("speakerNo")
        if value in (None, ""):
            match = re.match(r"^speaker:(\d+)$", str(voice_key or "").strip())
            value = match.group(1) if match else None
        if value in (None, ""):
            return None
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if number <= 0:
            return None
        return number

    @staticmethod
    def _normalize_composite_ui_text(value):
        return re.sub(r"\s+", "", str(value or "")).strip().casefold()

    @classmethod
    def _composite_ui_text_matches(cls, actual, expected):
        actual_text = cls._normalize_composite_ui_text(actual)
        expected_text = cls._normalize_composite_ui_text(expected)
        if not actual_text or not expected_text:
            return False
        if expected_text in actual_text:
            return True
        # 音色卡片有的版本把名称中的短横线渲染成空格，匹配时兼容
        # 这种展示差异，但仍要求完整音色名称出现在卡片文字中。
        compact_actual = actual_text.replace("-", "").replace("－", "")
        compact_expected = expected_text.replace("-", "").replace("－", "")
        return compact_expected in compact_actual

    @classmethod
    def _composite_voice_search_selector(cls):
        """返回多人配音弹层内的音色搜索框选择器。"""
        return (
            'input[placeholder*="搜索主播 / 标签"]:visible, '
            'input[placeholder*="搜索主播"]:visible, '
            'input[placeholder*="输入主播名称进行搜索"]:visible, '
            'input[placeholder*="输入主播名称"]:visible'
        )

    @classmethod
    def _composite_panel_scope(
        cls, page, require_apply_control=True, cancel_check=None
    ):
        """返回真正的多人配音弹层，不把右侧栏搜索框当成弹层。

        右侧普通音色栏和“多人配音”弹层都可能使用“搜索主播”占位符。
        只有位于可见弹层根节点、且提供“使用”操作的搜索框，才属于本
        自动化流程要操作的多人配音列表。
        """
        search_selector = cls._composite_voice_search_selector()
        roots = page.locator(
            'div.fixed:visible, [role="dialog"]:visible, .ant-modal:visible'
        )
        fallback = None
        try:
            for index in range(min(roots.count(), 20)):
                _check_cancel_requested(cancel_check)
                root = roots.nth(index)
                if root.locator(search_selector).count() == 0:
                    continue
                if fallback is None:
                    fallback = root
                if not require_apply_control:
                    return root

                controls = root.locator(
                    'button:visible, [role="button"]:visible, '
                    '[data-speaker-id]:visible, .cursor-pointer:visible'
                )
                metadata = controls.evaluate_all(
                    """els => els.map(el => ({
                        text: (el.innerText || '').trim(),
                        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                    }))"""
                )
                _check_cancel_requested(cancel_check)
                if any(
                    cls._normalize_composite_ui_text(item.get("text")) == "使用"
                    for item in metadata
                ):
                    return root
        except XunfeiCancelled:
            raise
        except Exception:
            pass
        return fallback if not require_apply_control else None

    @classmethod
    def _composite_panel_search(cls, page, cancel_check=None):
        """返回多人配音弹层搜索框；整页右侧栏搜索框不会被返回。"""
        scope = cls._composite_panel_scope(
            page,
            require_apply_control=True,
            cancel_check=cancel_check,
        )
        if scope is None:
            return None
        search = scope.locator(cls._composite_voice_search_selector())
        return search.first if search.count() > 0 else None

    @classmethod
    def _composite_ui_scope(cls, page, cancel_check=None):
        """返回当前多人配音弹层，避免点击被右侧栏或旧卡片拦截。"""
        return (
            cls._composite_panel_scope(
                page,
                require_apply_control=True,
                cancel_check=cancel_check,
            )
            or page
        )

    @classmethod
    def _click_composite_ui_control(cls, page, label, cancel_check=None):
        """点击可见的多人配音工具按钮，使用真实 Playwright click。"""
        _check_cancel_requested(cancel_check)
        expected = cls._normalize_composite_ui_text(label)
        # 停顿按钮会被连续点击很多次，但每次的可访问名称都稳定为
        # ``2s``/``1s`` 等。优先直接定位这个可见 UI 按钮，避免每处停顿
        # 都重新扫描整页几十个控件；点击仍是 Playwright 的真实 click，
        # 找不到时再走下面的严格元数据扫描兜底。
        if re.fullmatch(r"\d+(?:\.\d+)?s", expected):
            try:
                direct = page.get_by_role("button", name=label, exact=True).last
                direct.click(timeout=500)
                _check_cancel_requested(cancel_check)
                return True
            except XunfeiCancelled:
                raise
            except Exception:
                pass
        scope = cls._composite_ui_scope(page, cancel_check=cancel_check)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        try:
            # 逐个 inner_text/is_disabled 会产生大量 Playwright ↔ 浏览器
            # 往返，打包客户端里尤其明显。这里只把当前可见控件的必要
            # 元数据一次性读回，最终 click 仍然使用真实页面控件。
            metadata = controls.evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || '').trim(),
                    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                }))"""
            )
            _check_cancel_requested(cancel_check)
            for item in metadata[:200]:
                _check_cancel_requested(cancel_check)
                if cls._normalize_composite_ui_text(item.get("text")) != expected:
                    continue
                if item.get("disabled"):
                    continue
                controls.nth(int(item["index"])).click(timeout=5000)
                _check_cancel_requested(cancel_check)
                return True
        except XunfeiCancelled:
            raise
        except Exception:
            pass
        return False

    @staticmethod
    def _composite_pause_duration_candidates(value):
        """把讯飞停顿控件的展示/属性值转换为毫秒候选。

        讯飞页面的不同版本分别出现过 ``2s``、``2 秒``、``2000ms`` 和
        ``data-value=2000``。停顿菜单不是公开 API，不能只依赖其中一个
        文案或属性；这里仅解析明确的时长表达式，不做模糊的字符串包含。
        """
        text = str(value or "").strip().casefold()
        if not text:
            return []
        text = re.sub(r"\s+", "", text)
        candidates = []
        for match in re.finditer(
            r"(?<![\d.])(\d+(?:\.\d+)?)(ms|毫秒|s|秒)(?!\w)",
            text,
        ):
            number = float(match.group(1))
            unit = match.group(2)
            milliseconds = number if unit in {"ms", "毫秒"} else number * 1000
            if milliseconds.is_integer():
                candidates.append(int(milliseconds))
        clock = re.search(r"(?<!\d)(\d+)[:：](\d{1,2})(?!\d)", text)
        if clock:
            candidates.append(int(clock.group(1)) * 60_000 + int(clock.group(2)) * 1000)
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            number = float(text)
            if number.is_integer():
                # 裸数字在 data-value 中通常是毫秒，在 data-duration 中
                # 也可能表示秒；调用方会同时比较这两种解释。
                candidates.extend((int(number), int(number * 1000)))
        return sorted(set(candidates))

    @classmethod
    def _composite_pause_metadata_matches(cls, item, boundary_ms):
        """严格判断一个可见控件是否代表目标时长的停顿。"""
        expected = int(boundary_ms)
        if not isinstance(item, dict) or item.get("disabled") or item.get("isEditorMarker"):
            return False
        type_text = cls._normalize_composite_ui_text(item.get("dataType"))
        class_text = cls._normalize_composite_ui_text(item.get("className"))
        pause_type = bool(re.search(r"break|pause|停顿", f"{type_text} {class_text}"))
        values = [
            item.get("text"),
            item.get("ariaLabel"),
            item.get("title"),
            item.get("dataValue"),
            item.get("dataDuration"),
            item.get("dataMs"),
            item.get("dataTime"),
        ]
        for value in values:
            if expected in cls._composite_pause_duration_candidates(value):
                return True
            # 某些页面只在 data-type=break 节点上保留裸秒数，例如
            # data-value="2"；没有 break/pause 语义时不接受这种解释，
            # 避免误点其它带数字的工具控件。
            if pause_type:
                try:
                    number = float(str(value).strip())
                except (TypeError, ValueError):
                    number = None
                if number is not None and (
                    int(number) == expected or int(number * 1000) == expected
                ):
                    return True
        return False

    @staticmethod
    def _composite_pause_control_selector():
        """返回停顿工具/菜单的可见控件选择器。

        停顿菜单可能是 button，也可能是带 data-value 的 div；不能只查
        button，否则页面会显示工具栏却永远找不到可插入的 2s 选项。
        """
        return (
            'button:visible, [role="button"]:visible, '
            '[role="menuitem"]:visible, .ant-dropdown-menu-item:visible, '
            '.ant-menu-item:visible, li:visible, '
            '[data-type="break"]:visible, [data-type="pause"]:visible, '
            '[data-value]:visible, [data-duration]:visible, [data-ms]:visible, '
            '[data-time]:visible, [aria-label]:visible, [title]:visible, '
            '.cursor-pointer:visible'
        )

    @classmethod
    def _composite_pause_control_metadata(cls, page, cancel_check=None):
        _check_cancel_requested(cancel_check)
        controls = page.locator(cls._composite_pause_control_selector())
        metadata = controls.evaluate_all(
            """els => els.map((el, index) => ({
                index,
                tagName: el.tagName || '',
                role: el.getAttribute('role') || '',
                text: (el.innerText || el.textContent || '').trim(),
                ariaLabel: el.getAttribute('aria-label') || '',
                title: el.getAttribute('title') || '',
                dataType: el.getAttribute('data-type') || '',
                dataValue: el.getAttribute('data-value') || '',
                dataDuration: el.getAttribute('data-duration') || '',
                dataMs: el.getAttribute('data-ms') || '',
                dataTime: el.getAttribute('data-time') || '',
                className: typeof el.className === 'string' ? el.className : '',
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                isEditorMarker: !!el.closest('.ssml-editor') && (
                    el.matches('[data-type="break"], [data-type="pause"]')
                    || /break|pause/i.test(typeof el.className === 'string' ? el.className : '')
                ),
            }))"""
        )
        _check_cancel_requested(cancel_check)
        return controls, metadata if isinstance(metadata, list) else []

    @classmethod
    def _click_composite_pause_duration(
        cls, page, boundary_ms, *, restore_selection=None, cancel_check=None
    ):
        """在当前编辑器工具栏/弹出菜单中点击目标停顿时长。"""
        try:
            controls, metadata = cls._composite_pause_control_metadata(
                page, cancel_check=cancel_check
            )
        except XunfeiCancelled:
            raise
        except Exception:
            return False
        matches = [
            item for item in metadata[:400]
            if cls._composite_pause_metadata_matches(item, boundary_ms)
        ]
        # 真实 button/role button 优先，避免先点到带 title 的内部 span。
        matches.sort(
            key=lambda item: (
                0 if str(item.get("tagName") or "").upper() == "BUTTON" else 1,
                0 if str(item.get("role") or "") == "button" else 1,
                int(item.get("index", 0)),
            )
        )
        for item in matches:
            try:
                _check_cancel_requested(cancel_check)
                if callable(restore_selection):
                    restore_selection()
                controls.nth(int(item["index"])).click(timeout=3000)
                _check_cancel_requested(cancel_check)
                return True
            except XunfeiCancelled:
                raise
            except Exception:
                continue
        return False

    @classmethod
    def _click_composite_pause_control(
        cls, page, boundary_ms, *, restore_selection=None, cancel_check=None
    ):
        """打开必要的停顿菜单并点击目标时长，返回是否完成页面点击。

        老版本页面把 ``2s`` 直接放在工具栏，新版本先显示“停顿”按钮，
        点击后才挂载时长菜单。两条路径都必须使用真实 Playwright click；
        不调用页面内部 React 方法，也不伪造编辑器 DOM。
        """
        if cls._click_composite_pause_duration(
            page,
            boundary_ms,
            restore_selection=restore_selection,
            cancel_check=cancel_check,
        ):
            return True

        try:
            controls, metadata = cls._composite_pause_control_metadata(
                page, cancel_check=cancel_check
            )
        except XunfeiCancelled:
            raise
        except Exception:
            return False

        def is_pause_trigger(item):
            if not isinstance(item, dict) or item.get("disabled") or item.get("isEditorMarker"):
                return False
            labels = (
                str(item.get("text") or ""),
                str(item.get("ariaLabel") or ""),
                str(item.get("title") or ""),
            )
            normalized = " ".join(labels).casefold()
            return (
                "停顿" in normalized
                or "pause" in normalized
                or "insert break" in normalized
            )

        triggers = [item for item in metadata[:400] if is_pause_trigger(item)]
        triggers.sort(
            key=lambda item: (
                0 if str(item.get("tagName") or "").upper() == "BUTTON" else 1,
                0 if str(item.get("role") or "") == "button" else 1,
                int(item.get("index", 0)),
            )
        )
        for item in triggers:
            try:
                _check_cancel_requested(cancel_check)
                # 工具栏的 mousedown 可能会保存当前原生 Selection；先在
                # 同一个 contenteditable 中恢复精确的行尾折叠选区，避免
                # 点击“停顿”时编辑器拿到的是上一次音色面板的选区。
                if callable(restore_selection):
                    restore_selection()
                controls.nth(int(item["index"])).click(timeout=3000)
            except XunfeiCancelled:
                raise
            except Exception:
                continue
            # 规范的编辑器会在工具栏 mousedown 时保留 Selection，但部分
            # 页面版本只在菜单打开后才完成这个动作。回调只恢复浏览器
            # Selection，不修改页面内容；由调用方提供精确的目标正文。
            if callable(restore_selection):
                try:
                    restore_selection()
                except XunfeiCancelled:
                    raise
                except Exception:
                    pass
            target_clicked = _poll(
                lambda: cls._click_composite_pause_duration(
                    page,
                    boundary_ms,
                    restore_selection=restore_selection,
                    cancel_check=cancel_check,
                ),
                timeout=2,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            )
            if target_clicked:
                return True
            # 有的页面“停顿”按钮本身就是固定时长插入按钮，没有单独的
            # 菜单项。把这次真实点击交给后面的 DOM 回读判断；若没有
            # 产生目标标记，调用方会明确报错，不会继续提交。
            if not item.get("dataValue") and not item.get("dataDuration"):
                return True

        # 工具栏折叠时，停顿入口可能藏在“更多/展开”菜单里；只接受
        # 明确的更多工具文案，避免误点任务详情等其它“展开”按钮。
        def is_more_trigger(item):
            if not isinstance(item, dict) or item.get("disabled") or item.get("isEditorMarker"):
                return False
            value = " ".join(
                str(item.get(key) or "")
                for key in ("text", "ariaLabel", "title")
            ).casefold()
            return any(token in value for token in ("更多工具", "更多功能", "more tools", "more"))

        for item in [item for item in metadata[:400] if is_more_trigger(item)]:
            try:
                _check_cancel_requested(cancel_check)
                controls.nth(int(item["index"])).click(timeout=3000)
            except XunfeiCancelled:
                raise
            except Exception:
                continue
            if callable(restore_selection):
                try:
                    restore_selection()
                except XunfeiCancelled:
                    raise
                except Exception:
                    pass
            if _poll(
                lambda: cls._click_composite_pause_duration(
                    page,
                    boundary_ms,
                    restore_selection=restore_selection,
                    cancel_check=cancel_check,
                ),
                timeout=1.5,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            ):
                return True
        return False

    @classmethod
    def _find_composite_voice_card(cls, page, voice_name, cancel_check=None):
        """寻找当前搜索结果中唯一的目标音色卡片。

        多人配音弹层同时包含搜索结果、最近使用音色和参数快捷卡片。
        以前只按整张控件的包含文本匹配，搜索 ``Amanda`` 时可能先命中
        最近使用卡片，或者在同名前缀音色中选择到错误项。这里只接受带
        音色头像的候选，并优先选择搜索结果的非 button 卡片；候选仍然
        不唯一时优先按主名称精确匹配，避免 ``Amanda`` 同时命中
        ``Amanda-教育`` 等变体导致的长时间轮询。
        """
        _check_cancel_requested(cancel_check)
        scope = cls._composite_ui_scope(page, cancel_check=cancel_check)
        controls = scope.locator(
            'button:visible, [role="button"]:visible, [data-speaker-id]:visible, '
            '.cursor-pointer:visible'
        )
        try:
            metadata = controls.evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || '').trim(),
                    tagName: el.tagName || '',
                    className: typeof el.className === 'string' ? el.className : '',
                    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                    alt: el.querySelector('img[alt]')?.getAttribute('alt') || '',
                    label: el.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent?.trim() || '',
                }))"""
            )
        except XunfeiCancelled:
            raise
        except Exception:
            return None
        _check_cancel_requested(cancel_check)
        expected_norm = cls._normalize_composite_ui_text(voice_name)
        candidates = []
        for item in metadata[:300]:
            _check_cancel_requested(cancel_check)
            index = int(item["index"])
            text = str(item.get("text") or "")
            if not cls._composite_ui_text_matches(text, voice_name):
                continue
            normalized = cls._normalize_composite_ui_text(text)
            if normalized in {"多人配音", "使用"} or "使用" in normalized:
                continue
            if item.get("disabled"):
                continue
            alt = str(item.get("alt") or "")
            if not cls._composite_ui_text_matches(f"{alt} {text}", voice_name):
                continue
            label = str(item.get("label") or "")
            candidates.append({
                "tag": str(item.get("tagName") or "").upper(),
                "className": str(item.get("className") or ""),
                "control": controls.nth(index),
                "text": text,
                "alt": alt,
                "label": label,
            })

        if not candidates:
            return None

        # 讯飞当前页面的搜索结果是 div.w-full 卡片，最近使用列表是
        # button。保留同名情况下的搜索结果优先级，同时兼容未来把结果
        # 渲染成 button 的版本。
        preferred = [
            item for item in candidates
            if item["tag"] != "BUTTON" or "w-full" in item["className"]
        ]
        pool = preferred or candidates
        if len(pool) == 1:
            return pool[0]["control"]

        # 多候选时优先按主名称精确匹配，避免 Amanda 误命中 Amanda-教育
        def _norm(value):
            return cls._normalize_composite_ui_text(value)

        exact = []
        for item in pool:
            label_norm = _norm(item.get("label") or "")
            alt_norm = _norm(item.get("alt") or "")
            text_norm = _norm(item.get("text") or "")
            # label 优先精确，其次 alt 精确，其次 alt 以 "-name" 结尾的 token 精确
            if label_norm == expected_norm:
                exact.append(item)
                continue
            if alt_norm == expected_norm:
                exact.append(item)
                continue
            # 兼容 alt 为 "英语-Amanda" 这类前缀形式
            if alt_norm and expected_norm and alt_norm.split("-")[-1] == expected_norm:
                exact.append(item)
                continue
            if alt_norm and expected_norm and alt_norm.split("－")[-1] == expected_norm:
                exact.append(item)
                continue
            # 全文本恰好等于期望时也算精确（无额外描述的卡片）
            if text_norm == expected_norm:
                exact.append(item)
                continue

        if len(exact) == 1:
            return exact[0]["control"]
        if len(exact) > 1:
            # 多个精确同名（极少见的重复 DOM），优先取第一个 w-full 结果
            # 避免返回 None 导致上层长时间轮询 5 秒
            _log(f"[xunfei]   多人配音音色精确候选仍不唯一: {voice_name}（{len(exact)} 项），取首个")
            return exact[0]["control"]

        # 无精确匹配时，按主标签长度启发式选择最接近的候选，避免长时间轮询
        # 例如搜索 Amanda 时，Amanda(6) 比 Amanda-教育(10) 更短
        def _score(item):
            label_norm = _norm(item.get("label") or "")
            # 优先用 label 长度，其次用 text 长度
            primary = label_norm if label_norm else _norm(item.get("text") or "")
            return len(primary)

        pool_sorted = sorted(pool, key=_score)
        if len(pool_sorted) >= 2 and _score(pool_sorted[0]) < _score(pool_sorted[1]):
            _log(
                f"[xunfei]   多人配音音色候选按长度启发式选择: {voice_name} -> "
                f"{pool_sorted[0].get('label') or pool_sorted[0].get('alt') or pool_sorted[0].get('text')[:20]!r}"
            )
            return pool_sorted[0]["control"]

        # 仍无法唯一确定时（多个候选长度相同等极少见情况），为避免上层
        # 轮询 5 秒后才重试，直接取首个并记录，避免用户感知到 2-4 秒停顿
        _log(
            f"[xunfei]   多人配音音色候选仍不唯一但已无法按长度区分: {voice_name}（{len(pool)} 项），取首个"
        )
        return pool_sorted[0]["control"]

    @classmethod
    def _open_composite_voice_panel(cls, page, cancel_check=None):
        """打开“多人配音”面板，并返回其搜索框。"""
        _check_cancel_requested(cancel_check)
        search = cls._composite_panel_search(page, cancel_check=cancel_check)
        if search is None:
            # 队列刚完成时工具栏按钮可能有几十到几百毫秒的 disabled
            # 状态。立即判失败会触发整组重试，客户端看起来就会慢很多；
            # 这里只等待按钮真正可用，正常路径第一次轮询即完成。
            clicked = _poll(
                lambda: (
                    True
                    if cls._click_composite_ui_control(
                        page, "多人配音", cancel_check=cancel_check
                    )
                    else None
                ),
                timeout=4,
                interval=0.08,
                max_interval=0.3,
                page=page,
                cancel_check=cancel_check,
            )
            if not clicked:
                raise XunfeiError("未找到可用的“多人配音”按钮")
            search = _poll(
                lambda: cls._composite_panel_search(
                    page, cancel_check=cancel_check
                ),
                timeout=8,
                interval=0.08,
                max_interval=0.4,
                page=page,
                cancel_check=cancel_check,
            )
        if search is None or search.count() == 0:
            raise XunfeiError("“多人配音”面板未加载音色搜索框")
        return search

    @classmethod
    def _close_composite_voice_panel(cls, page, cancel_check=None):
        """关闭多人配音音色面板，避免失败重试时遮挡编辑器。

        音色卡片搜索失败时，讯飞页面仍会保留一个 fixed 遮罩层。这个
        遮罩层会拦截编辑器的真实 click，导致后续重新输入看起来像是
        编辑器坏了。优先使用页面支持的 Escape，再在仍可见时点击面板
        内的明确关闭/取消控件；整个过程只使用浏览器可见 UI 操作。
        """
        _check_cancel_requested(cancel_check)
        search = cls._composite_panel_search(page, cancel_check=cancel_check)

        def panel_closed():
            _check_cancel_requested(cancel_check)
            return (
                cls._composite_panel_search(page, cancel_check=cancel_check)
                is None
            )

        if panel_closed():
            return True
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if _poll(
            panel_closed,
            timeout=1.5,
            interval=0.1,
            max_interval=0.4,
            page=page,
            cancel_check=cancel_check,
        ):
            return True

        # 某些页面版本不响应 Escape，但面板会渲染“关闭/取消”按钮。
        # 只在包含音色搜索框的 fixed 弹层内匹配，避免误点编辑器其它按钮。
        try:
            scope = cls._composite_panel_scope(
                page,
                require_apply_control=True,
                cancel_check=cancel_check,
            )
            if scope is None or search is None:
                return panel_closed()
            controls = scope.locator(
                'button:visible, [role="button"]:visible'
            )
            close_labels = {"关闭", "取消", "×", "✕", "close", "cancel"}
            for index in range(min(controls.count(), 100)):
                _check_cancel_requested(cancel_check)
                control = controls.nth(index)
                label = ""
                try:
                    label = (control.inner_text(timeout=500) or "").strip()
                except Exception:
                    pass
                aria = (control.get_attribute("aria-label") or "").strip()
                title = (control.get_attribute("title") or "").strip()
                if not any(
                    value.casefold() in close_labels
                    for value in (label, aria, title)
                    if value
                ):
                    continue
                control.click(timeout=3000)
                if _poll(
                    panel_closed,
                    timeout=1.5,
                    interval=0.1,
                    max_interval=0.4,
                    page=page,
                    cancel_check=cancel_check,
                ):
                    return True
        except XunfeiCancelled:
            raise
        except Exception:
            pass
        return panel_closed()

    @classmethod
    def _apply_composite_ui_params(
        cls, page, speed, pitch, volume, *, cancel_check=None
    ):
        """在多人配音面板中用键盘设置三项参数并逐项回读。"""
        _check_cancel_requested(cancel_check)
        targets = (
            clamp_param(speed),
            clamp_param(pitch),
            clamp_param(volume),
        )
        labels = ("语速", "语调", "音量")
        scope = cls._composite_ui_scope(page, cancel_check=cancel_check)

        def find_inputs():
            inputs = scope.locator('input.w-12:visible')
            if inputs.count() >= 3:
                return inputs
            inputs = scope.locator('input[placeholder="数值"]:visible')
            return inputs if inputs.count() >= 3 else None

        inputs = _poll(
            find_inputs,
            timeout=8,
            interval=0.25,
            max_interval=0.8,
            page=page,
            cancel_check=cancel_check,
        )
        if inputs is None or inputs.count() < 3:
            raise XunfeiError("“多人配音”面板的语速、语调、音量输入框未完整加载")

        for index, (label, value) in enumerate(zip(labels, targets)):
            _check_cancel_requested(cancel_check)
            field = inputs.nth(index)

            def read_expected_value():
                try:
                    actual_value = field.input_value(timeout=1000).strip()
                except Exception:
                    return None
                return actual_value if actual_value == str(value) else None

            try:
                field.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.type(str(value))
                page.keyboard.press("Tab")
                # 输入框的 DOM value 会先于讯飞 React 表单状态更新；
                # 不能只看到 input_value 正确就立即点击“使用”，否则
                # 会把上一组音色的旧参数带入标记。80ms 足够让 blur/input
                # 状态落地，仍比原先每项固定 180ms 更快。
                _wait_with_cancel(page, 0.08, cancel_check=cancel_check)
                _check_cancel_requested(cancel_check)
                actual = _poll(
                    read_expected_value,
                    timeout=1.2,
                    interval=0.025,
                    max_interval=0.12,
                    page=page,
                    cancel_check=cancel_check,
                )
            except XunfeiCancelled:
                raise
            except Exception as error:
                raise XunfeiError(
                    f"多人配音 UI 参数[{label}]设置失败: {error}"
                ) from error
            if actual != str(value):
                raise XunfeiError(
                    f"多人配音 UI 参数[{label}]回读不一致："
                    f"期望 {value}，实际 {actual!r}"
                )

    @classmethod
    def _composite_row_signature(cls, row):
        return (
            str(row.get("voice_key") or DEFAULT_FEMALE),
            clamp_param(row.get("speed", PARAM_DEFAULT)),
            clamp_param(row.get("pitch", PARAM_DEFAULT)),
            clamp_param(row.get("volume", PARAM_DEFAULT)),
        )

    @classmethod
    def _composite_row_groups(cls, rows):
        """把相邻且配置完全相同的编辑器行合并为一次选区操作。"""
        if not rows:
            return []
        groups = []
        start = 0
        previous = cls._composite_row_signature(rows[0])
        for index in range(1, len(rows)):
            current = cls._composite_row_signature(rows[index])
            if current != previous:
                groups.append((start, index - 1))
                start = index
                previous = current
        groups.append((start, len(rows) - 1))
        return groups

    @classmethod
    def _composite_signature_ranges(cls, rows):
        """按最终配置收集不连续的连续区间，供讯飞多段选择队列使用。

        讯飞编辑器支持按住 Command/Ctrl 依次加入多个不连续选区，随后
        对队列统一套用音色和三项参数。这里保留连续区间边界用于规划、
        校验和日志统计；实际长文档按行用浏览器原生精确选区加入队列，
        避免跨滚动或换行造成误选，同时不再用全文覆盖去修正例外。
        """
        groups = {}
        order = []
        for first_index, last_index in cls._composite_row_groups(rows):
            signature = cls._composite_row_signature(rows[first_index])
            if signature not in groups:
                groups[signature] = []
                order.append(signature)
            groups[signature].append((first_index, last_index))
        return [
            {
                "signature": signature,
                "ranges": groups[signature],
            }
            for signature in order
        ]

    @classmethod
    def _composite_marking_plan(cls, rows):
        """生成多人配音的低交互次数标注计划。

        Chrome 的原生 Selection 只有一个连续 Range，不能安全地把交错的
        多个非连续段落同时交给讯飞页面。因此这里采用“基准覆盖 + 例外
        修正”：先把全文一次性设置为出现次数最多的完整配置，再按连续
        区间修正其它配置。这样既保留真实页面选区，也避免 W/M 交替时为
        每一行重复打开音色面板。
        """
        if not rows:
            return {
                "base_index": None,
                "base_signature": None,
                "correction_groups": [],
                "contiguous_group_count": 0,
            }

        counts = {}
        first_indices = {}
        for index, row in enumerate(rows):
            signature = cls._composite_row_signature(row)
            counts[signature] = counts.get(signature, 0) + 1
            first_indices.setdefault(signature, index)
        base_signature = max(
            counts,
            key=lambda signature: (
                counts[signature],
                -first_indices[signature],
            ),
        )
        base_index = first_indices[base_signature]

        correction_groups = []
        start = None
        for index, row in enumerate(rows):
            if cls._composite_row_signature(row) == base_signature:
                if start is not None:
                    correction_groups.append((start, index - 1))
                    start = None
            elif start is None:
                start = index
        if start is not None:
            correction_groups.append((start, len(rows) - 1))

        return {
            "base_index": base_index,
            "base_signature": base_signature,
            "correction_groups": correction_groups,
            "contiguous_group_count": len(cls._composite_row_groups(rows)),
        }

    @classmethod
    def _composite_ui_rows(cls, work):
        """展开作品为编辑器行，并记录每道题之后需要插入的停顿位置。"""
        rows = []
        item_last_indices = []
        items = list(work.get("items") or [])
        if not items:
            raise XunfeiError("多人配音作品没有可合成的题目")

        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                raise XunfeiError(f"多人配音第 {item_index + 1} 道题数据异常")
            segments = item.get("segments") or []
            if not segments and item.get("text"):
                # 兼容旧的/恢复中的多人配音计划：这类条目可能只有
                # 原始 text，没有经过 composite_plan 预展开。必须先走
                # 统一的 W/M 解析，否则 (W)/(M) 会被原样输入讯飞编辑器
                # 并可能被当作正文朗读。
                from wordtts.synthesis import build_synthesis_segments

                segments = build_synthesis_segments(
                    item.get("text"),
                    item.get("speed", item.get("rate", PARAM_DEFAULT)),
                    item.get("volume", PARAM_DEFAULT),
                    item.get("pitch", PARAM_DEFAULT),
                    default_voice=item.get("default_voice") or item.get("voice_key"),
                    female_voice=item.get("female_voice"),
                    male_voice=item.get("male_voice"),
                    role_voices=item.get("role_voices"),
                    role_configs=item.get("role_configs"),
                    default_role=item.get("default_role"),
                )
            before = len(rows)
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text") or "")
                if not text.strip():
                    continue
                voice_key = str(segment.get("voice_key") or DEFAULT_FEMALE).strip()
                # 目录校验提前进行，避免已经输入文本后才发现音色 key 无效。
                get_voice_info(voice_key)
                lines = text.splitlines() or [text]
                for line in lines:
                    clean = line.strip()
                    if not clean:
                        continue
                    rows.append({
                        "item_index": item_index,
                        "text": clean,
                        "voice_key": voice_key,
                        "speed": clamp_param(segment.get("speed", PARAM_DEFAULT)),
                        "pitch": clamp_param(segment.get("pitch", PARAM_DEFAULT)),
                        "volume": clamp_param(segment.get("volume", PARAM_DEFAULT)),
                    })
            if len(rows) == before:
                raise XunfeiError(
                    f"多人配音第 {item_index + 1} 道题没有可合成的文本"
                )
            item_last_indices.append(len(rows) - 1)

        boundary_ms = int(work.get("boundary_ms") or 2000)
        boundaries = [
            (last_index, boundary_ms)
            for last_index in item_last_indices[:-1]
        ]
        return rows, boundaries

    @classmethod
    def _verify_composite_voice_marks(
        cls, page, rows, first_index, last_index, voice_name, speaker_number,
        config_row=None,
    ):
        """确认目标行只有一个完整、正确的音色标记。"""
        return cls._verify_composite_voice_marks_ranges(
            page,
            rows,
            [(first_index, last_index)],
            voice_name,
            speaker_number,
            config_row,
        )

    @classmethod
    def _verify_composite_voice_marks_ranges(
        cls, page, rows, ranges, voice_name, speaker_number, config_row=None
    ):
        """确认多个不连续选区中的每一行都只保留目标音色标记。"""
        del voice_name  # 仅用于保留原调用签名，页面回读以 speakerNo 为准。
        expected_indices = [
            index
            for first_index, last_index in ranges
            for index in range(first_index, last_index + 1)
        ]
        if not expected_indices:
            return False
        try:
            snapshot = page.evaluate(
                """indices => indices.map(index => {
                    const paragraph = document.querySelectorAll('.ssml-editor p')[index];
                    if (!paragraph) return {index, paragraph: false};
                    const marks = Array.from(
                        paragraph.querySelectorAll('.ssml-text-mark-speaker')
                    );
                    return {
                        index,
                        paragraph: true,
                        markCount: marks.length,
                        marks: marks.map(mark => {
                            const content = Array.from(
                                mark.querySelectorAll(
                                    'span.range-annotation-content.speaker-content'
                                )
                            ).filter(el => (
                                !el.classList.contains('ssml-tag')
                                && el.getAttribute('data-type') !== 'range_anchor'
                            ));
                            return {
                                speakerId: mark.getAttribute('data-speaker-id') || '',
                                rate: mark.getAttribute('data-rate') || '',
                                pitch: mark.getAttribute('data-pitch') || '',
                                volume: mark.getAttribute('data-volume') || '',
                                contentCount: content.length,
                                contentText: content.length === 1
                                    ? (content[0].textContent || '')
                                    : '',
                            };
                        }),
                    };
                })""",
                expected_indices,
            )
        except Exception:
            return False
        if not isinstance(snapshot, list) or len(snapshot) != len(expected_indices):
            return False

        expected_id = (
            str(speaker_number)
            if speaker_number not in (None, "", 0)
            else None
        )
        expected_params = None
        if config_row is not None:
            expected_params = {
                "rate": str(clamp_param(config_row.get("speed", PARAM_DEFAULT))),
                "pitch": str(clamp_param(config_row.get("pitch", PARAM_DEFAULT))),
                "volume": str(clamp_param(config_row.get("volume", PARAM_DEFAULT))),
            }
        for item, index in zip(snapshot, expected_indices):
            # 一个逻辑行只能有一个完整 speaker mark。只要保留旧标记、
            # 产生混合标记或标记被截成两段，都必须失败，不能“有一个对的
            # 标记就算通过”，否则最终作品会出现错音色片段。
            if not item.get("paragraph") or item.get("markCount") != 1:
                return False
            mark = item.get("marks", [None])[0]
            if not isinstance(mark, dict):
                return False
            # common/list 只返回基础音色 commonId，不返回具体变体的
            # speakerNo；这时由讯飞页面在点击基础卡片后选择实际 speakerId，
            # 只要求回读到非空 ID。旧 flat/内置目录仍继续做精确 ID 校验。
            if expected_id is not None and mark.get("speakerId") != expected_id:
                return False
            if expected_id is None and not str(mark.get("speakerId") or "").strip():
                return False
            if expected_params is not None:
                for attribute, expected_value in expected_params.items():
                    if mark.get(attribute) != expected_value:
                        return False
            if mark.get("contentCount") != 1:
                return False
            expected_text = str(rows[index].get("text") or "")
            if cls._normalize_selection_text(mark.get("contentText")) != cls._normalize_selection_text(expected_text):
                return False
        return True

    @classmethod
    def _apply_composite_voice_to_selection(
        cls, page, rows, first_index, last_index, *, config_row=None,
        verify_ranges=None, cancel_check=None,
    ):
        """给当前精确选区设置音色、参数，并回读页面的 speaker 标记。"""
        _check_cancel_requested(cancel_check)
        first_row = config_row or rows[first_index]
        voice_key = str(first_row.get("voice_key") or DEFAULT_FEMALE)
        info = dict(get_voice_info(voice_key))
        voice_name = str(info.get("name") or voice_key)
        speaker_number = cls._speaker_number(voice_key, info)
        if config_row is None:
            for index in range(first_index, last_index + 1):
                if cls._composite_row_signature(rows[index]) != cls._composite_row_signature(first_row):
                    raise XunfeiError("多人配音批量选区包含不同音色或参数，拒绝套用")

        phase_started_at = time.perf_counter()
        search = cls._open_composite_voice_panel(page, cancel_check=cancel_check)
        panel_open_ms = round((time.perf_counter() - phase_started_at) * 1000)
        card = None
        for search_attempt in range(2):
            _check_cancel_requested(cancel_check)
            search.click(timeout=3000)
            page.keyboard.press(_SELECT_ALL)
            page.keyboard.type(voice_name)
            card = _poll(
                lambda: cls._find_composite_voice_card(
                    page, voice_name, cancel_check=cancel_check
                ),
                timeout=4,
                interval=0.08,
                max_interval=0.35,
                page=page,
                cancel_check=cancel_check,
            )
            if card is not None:
                break
            if search_attempt == 0:
                # 搜索结果偶尔会因弹层刚打开而没有挂载。重新打开同一
                # 个网页面板即可恢复，不改变编辑器选区，也不盲点其它
                # 音色卡片。
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                search = cls._open_composite_voice_panel(page, cancel_check=cancel_check)
        if card is None:
            raise XunfeiError(f"多人配音面板未找到音色卡片: {voice_name}")
        _check_cancel_requested(cancel_check)
        card.click(timeout=5000)
        # 选中卡片后面板会重新挂载三项参数输入框；输入框数量出现
        # 之前，旧的输入节点也可能短暂可见。给 React 一次短落地时间，
        # 避免把参数发给上一张卡片的旧表单。
        _wait_with_cancel(page, 0.08, cancel_check=cancel_check)
        _check_cancel_requested(cancel_check)
        card_ms = round((time.perf_counter() - phase_started_at) * 1000)
        params_started_at = time.perf_counter()
        cls._apply_composite_ui_params(
            page,
            first_row.get("speed", PARAM_DEFAULT),
            first_row.get("pitch", PARAM_DEFAULT),
            first_row.get("volume", PARAM_DEFAULT),
            cancel_check=cancel_check,
        )
        params_ms = round((time.perf_counter() - params_started_at) * 1000)
        apply_started_at = time.perf_counter()
        _check_cancel_requested(cancel_check)
        if not cls._click_composite_ui_control(
            page, "使用", cancel_check=cancel_check
        ):
            raise XunfeiError(f"多人配音面板未找到可用的“使用”按钮: {voice_name}")
        ranges_to_verify = verify_ranges or [(first_index, last_index)]
        verified = _poll(
            lambda: cls._verify_composite_voice_marks_ranges(
                page,
                rows,
                ranges_to_verify,
                voice_name,
                speaker_number,
                first_row,
            ),
            timeout=8,
            interval=0.2,
            max_interval=0.8,
            page=page,
            cancel_check=cancel_check,
        )
        if not verified:
            raise XunfeiError(
                f"多人配音音色标记回读失败：行 {first_index + 1}-{last_index + 1} "
                f"未确认使用 {voice_name}"
            )
        apply_ms = round((time.perf_counter() - apply_started_at) * 1000)
        # 讯飞页面对包含连续范围的队列有时会保留 pending-range 装饰，
        # 虽然音色已经成功套用。显式用 Escape 清理网页队列，确保下一
        # 个音色配置组不会把上一组的待处理段落一起带入。
        if verify_ranges and not cls._clear_composite_queue(page, cancel_check=cancel_check):
            raise XunfeiError("多人配音上一组多段选区未能清理")
        _log(
            f"[xunfei]   多人配音配置细分 {voice_name}："
            f"面板 {panel_open_ms}ms，音色卡片 {card_ms - panel_open_ms}ms，"
            f"参数 {params_ms}ms，应用回读 {apply_ms}ms"
        )
        if not verify_ranges:
            _log(
                f"[xunfei]   多人配音已设置行 {first_index + 1}-{last_index + 1}: "
                f"{voice_name}, speed={clamp_param(first_row.get('speed'))}, "
                f"pitch={clamp_param(first_row.get('pitch'))}, "
                f"volume={clamp_param(first_row.get('volume'))}"
            )

    @classmethod
    def _apply_composite_voice_to_queue(
        cls, page, rows, ranges, *, cancel_check=None
    ):
        """对讯飞网页多段选择队列一次性设置音色和三项参数。"""
        _check_cancel_requested(cancel_check)
        if not ranges:
            raise XunfeiError("多人配音多段选区没有可套用的音色配置")
        first_index = ranges[0][0]
        first_row = rows[first_index]
        expected_signature = cls._composite_row_signature(first_row)
        if any(
            cls._composite_row_signature(rows[index]) != expected_signature
            for first, last in ranges
            for index in range(first, last + 1)
        ):
            raise XunfeiError("多人配音多段选区包含不同音色或参数，拒绝套用")

        cls._apply_composite_voice_to_selection(
            page,
            rows,
            first_index,
            ranges[0][1],
            config_row=first_row,
            verify_ranges=ranges,
            cancel_check=cancel_check,
        )
        _log(
            f"[xunfei]   多人配音已统一设置 {len(ranges)} 个区间、"
            f"{sum(last - first + 1 for first, last in ranges)} 行: "
            f"{get_voice_info(first_row.get('voice_key') or DEFAULT_FEMALE)['name']}"
        )

    @classmethod
    def _read_composite_pause_issues(cls, page, boundaries):
        """一次回读所有停顿标记，避免每个段落都单独查询 DOM。"""
        expected = [
            {"row": int(row_index), "value": str(int(boundary_ms))}
            for row_index, boundary_ms in boundaries
        ]
        try:
            result = page.evaluate(
                """expected => {
                    const normalize = (raw) => String(raw || '')
                        .replace(/\\s+/g, '')
                        .toLowerCase();
                    const parseDurations = (raw) => {
                        const text = normalize(raw);
                        if (!text) return [];
                        const values = [];
                        for (const match of text.matchAll(
                            /(?<![\\d.])(\\d+(?:\\.\\d+)?)(ms|毫秒|s|秒)(?!\\w)/g
                        )) {
                            const number = Number(match[1]);
                            values.push((match[2] === 'ms' || match[2] === '毫秒')
                                ? number : number * 1000);
                        }
                        const clock = text.match(/(?<!\\d)(\\d+)[:：](\\d{1,2})(?!\\d)/);
                        if (clock) values.push(Number(clock[1]) * 60000 + Number(clock[2]) * 1000);
                        if (/^\\d+(?:\\.\\d+)?$/.test(text)) {
                            const number = Number(text);
                            values.push(number, number * 1000);
                        }
                        return values.filter(Number.isFinite);
                    };
                    const isTargetPause = (el, value) => {
                        const type = normalize(el.getAttribute('data-type'));
                        const className = normalize(
                            typeof el.className === 'string' ? el.className : ''
                        );
                        const pauseType = /break|pause|停顿/.test(`${type} ${className}`);
                        if (!pauseType) return false;
                        const rawValues = [
                            el.getAttribute('data-value'),
                            el.getAttribute('data-duration'),
                            el.getAttribute('data-ms'),
                            el.getAttribute('data-time'),
                            el.getAttribute('aria-label'),
                            el.getAttribute('title'),
                            el.textContent,
                        ];
                        return rawValues.some((raw) => parseDurations(raw).some(
                            (duration) => Math.round(duration) === Number(value)
                        ));
                    };
                    const paragraphs = document.querySelectorAll('.ssml-editor p');
                    return expected.map(({row, value}) => {
                        const paragraph = paragraphs[row];
                        const nodes = paragraph
                            ? [paragraph, ...paragraph.querySelectorAll('*')]
                            : [];
                        const count = nodes.filter((el) => isTargetPause(el, value)).length;
                        return {row, value, count};
                    }).filter(item => item.count !== 1);
                }""",
                expected,
            )
        except Exception:
            return expected
        return result if isinstance(result, list) else expected

    @classmethod
    def _insert_composite_pause(
        cls, page, row_index, boundary_ms, *, emit_log=True, verify=True,
        cancel_check=None,
    ):
        """在指定题目末行末尾通过页面停顿按钮插入内部定位标记。"""
        _check_cancel_requested(cancel_check)
        paragraphs = page.locator(".ssml-editor p")
        if row_index < 0 or row_index >= paragraphs.count():
            raise XunfeiError("多人配音停顿位置超出编辑器段落范围")
        label = f"{int(boundary_ms) / 1000:g}s"

        def marker_present():
            return not cls._read_composite_pause_issues(page, [(row_index, boundary_ms)])

        def settled(clicked):
            # verify=False 的批量路径依赖随后的整批回读修复；单次插入只
            # 以点击成功为完成条件。verify=True 时必须回读到标记才算数。
            if not clicked:
                return False
            return not verify or marker_present()

        def log_inserted():
            if emit_log:
                _log(f"[xunfei]   已在第 {row_index + 1} 行后插入 {label} 停顿")

        # 主路径：一次 evaluate 把原生光标折叠到该行末尾（等价于原脚本
        # “选整行 -> ArrowRight”的落点，但省掉两三次重型 select_text
        # 往返），再用一步定位的真实 click 点击停顿时长按钮。是否真正
        # 插入由回读校验决定，失败才逐级降级。
        _check_cancel_requested(cancel_check)
        try:
            placed = page.evaluate(JS.PLACE_CARET_AT_ROW_END, row_index)
        except Exception:
            placed = None
        if isinstance(placed, dict) and placed.get("ok"):
            _check_cancel_requested(cancel_check)
            if settled(
                cls._click_composite_ui_control(
                    page, label, cancel_check=cancel_check
                )
            ):
                _check_cancel_requested(cancel_check)
                log_inserted()
                return

        # 原脚本契约回退：JS 选整行 -> ArrowRight 折叠到行尾。程序化
        # 光标不被页面认账时，这个方向键折叠仍然有效。
        _check_cancel_requested(cancel_check)
        fast_box = _safe_eval(page, JS.SELECT_EDITOR_ROW, row_index)
        if not isinstance(fast_box, dict) or not fast_box.get("text"):
            paragraphs.nth(row_index).select_text(timeout=5000)
        page.keyboard.press("ArrowRight")
        _wait_with_cancel(page, 0.01, cancel_check=cancel_check)
        _check_cancel_requested(cancel_check)
        if settled(
            cls._click_composite_ui_control(
                page, label, cancel_check=cancel_check
            )
        ):
            _check_cancel_requested(cancel_check)
            log_inserted()
            return

        # 最终兜底：原生 select_text 保证焦点、选区和页面键盘事件属于
        # 同一个 contenteditable，再配合整页元数据扫描和停顿菜单路径。
        paragraph = paragraphs.nth(row_index)
        target = paragraph
        try:
            content = paragraph.locator(
                'span.range-annotation-content.speaker-content'
                ':not(.ssml-tag):not([data-type="range_anchor"]):visible'
            )
            if content.count() == 1:
                target = content.first
        except Exception:
            target = paragraph
        try:
            target.scroll_into_view_if_needed(timeout=5000)
            target.select_text(timeout=5000)
        except Exception:
            # 页面版本可能没有 speaker-content 包裹未标注正文；整段 p
            # 仍然是同一个编辑器原生选区，作为安全回退。
            paragraph.scroll_into_view_if_needed(timeout=5000)
            paragraph.select_text(timeout=5000)

        def collapse_pause_selection_to_end():
            """重建原脚本的“选整行 -> ArrowRight -> 行尾”契约。"""
            _check_cancel_requested(cancel_check)
            try:
                target.scroll_into_view_if_needed(timeout=5000)
                target.select_text(timeout=5000)
            except Exception:
                paragraph.scroll_into_view_if_needed(timeout=5000)
                paragraph.select_text(timeout=5000)
            page.keyboard.press("ArrowRight")
            # select_text + ArrowRight 都是同步的浏览器输入动作；给页面
            # 一个很短的事件循环机会，实际插入结果由下面的回读轮询确认。
            _wait_with_cancel(page, 0.02, cancel_check=cancel_check)
            _check_cancel_requested(cancel_check)

        collapse_pause_selection_to_end()

        def restore_selection():
            collapse_pause_selection_to_end()

        clicked = _poll(
            lambda: (
                True
                if cls._click_composite_pause_control(
                    page,
                    boundary_ms,
                    restore_selection=restore_selection,
                    cancel_check=cancel_check,
                )
                else None
            ),
            timeout=2.5,
            interval=0.04,
            max_interval=0.2,
            page=page,
            cancel_check=cancel_check,
        )
        if not clicked:
            raise XunfeiError(f"未找到讯飞停顿按钮或时长菜单: {label}")
        if verify:
            inserted = _poll(
                lambda: not cls._read_composite_pause_issues(
                    page, [(row_index, boundary_ms)]
                ),
                timeout=3,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            )
            if not inserted:
                raise XunfeiError(
                    f"讯飞停顿插入校验失败：第 {row_index + 1} 行未找到 {boundary_ms}ms 标记"
                )
        log_inserted()

    @classmethod
    def _prepare_composite_editor(cls, page, work, *, cancel_check=None):
        """用讯飞页面 UI 构造多人作品，返回行和停顿边界。"""
        _check_cancel_requested(cancel_check)
        started_at = time.perf_counter()
        rows, boundaries = cls._composite_ui_rows(work)
        cls._input_composite_text(page, rows, cancel_check=cancel_check)
        _check_cancel_requested(cancel_check)
        groups = cls._composite_row_groups(rows)
        queue_plan = cls._composite_signature_ranges(rows)
        _log(
            f"[xunfei]   多人配音 UI 已输入 {len(rows)} 行，"
            f"原连续配置 {len(groups)} 组；多段队列将按 "
            f"{len(queue_plan)} 个音色/参数组统一标注"
        )

        # 讯飞新版编辑器提供真实的 Command/Ctrl 多段选择队列：同一配置的
        # 不连续行先全部加入队列，再一次点击“使用”统一设置音色和参数。
        # 若页面版本没有该能力或队列回读失败，重新输入文本后退回旧的
        # 连续区间方案，保证正确性优先。
        queue_error = None
        # 正常路径使用页面 Range 建立选区，遇到页面版本不接受 Range
        # 时，后续整批都切换为原生 select_text，避免在同一批任务中反复
        # 试探两种选区机制。
        native_selection = False
        for queue_attempt in range(2):
            _check_cancel_requested(cancel_check)
            if queue_attempt:
                native_selection = True
                _log(
                    "[xunfei]   多人配音多段队列应用回读失败，"
                    "重新输入全部文本后再试一次"
                )
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._clear_composite_queue(page, cancel_check=cancel_check)
                cls._input_composite_text(page, rows, cancel_check=cancel_check)
            try:
                for entry_index, entry in enumerate(queue_plan, start=1):
                    _check_cancel_requested(cancel_check)
                    group_started_at = time.perf_counter()
                    ranges = entry["ranges"]
                    selection_error = None
                    for selection_attempt in range(2):
                        _check_cancel_requested(cancel_check)
                        try:
                            cls._select_composite_queue_rows(
                                page,
                                rows,
                                ranges,
                                native=(native_selection or selection_attempt > 0),
                                cancel_check=cancel_check,
                            )
                            selection_error = None
                            break
                        except XunfeiCancelled:
                            raise
                        except XunfeiError as error:
                            selection_error = error
                            retryable = (
                                "多人配音 UI 选区校验失败" in str(error)
                                or "多人配音多段选区数量校验失败" in str(error)
                                or "多人配音快速选区" in str(error)
                            )
                            if selection_attempt == 0 and retryable:
                                native_selection = True
                                _log(
                                    "[xunfei]   多人配音多段选区回读不一致，"
                                    "清空当前队列后重试一次"
                                )
                                if not cls._clear_composite_queue(
                                    page, cancel_check=cancel_check
                                ):
                                    break
                                continue
                            break
                    if selection_error:
                        raise selection_error
                    cls._apply_composite_voice_to_queue(
                        page, rows, ranges, cancel_check=cancel_check
                    )
                    _check_cancel_requested(cancel_check)
                    voice_name = get_voice_info(
                        rows[ranges[0][0]].get("voice_key") or DEFAULT_FEMALE
                    )["name"]
                    group_duration_ms = round(
                        (time.perf_counter() - group_started_at) * 1000
                    )
                    _log(
                        f"[xunfei]   多人配音配置组 {entry_index}/{len(queue_plan)} "
                        f"已完成：{voice_name}，{sum(last - first + 1 for first, last in ranges)} 行，"
                        f"耗时 {group_duration_ms}ms"
                    )
                queue_error = None
                break
            except XunfeiCancelled:
                raise
            except XunfeiError as error:
                queue_error = error
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._clear_composite_queue(page, cancel_check=cancel_check)

        try:
            if queue_error:
                raise queue_error
        except XunfeiCancelled:
            raise
        except XunfeiError as error:
            _log(
                f"[xunfei]   多人配音多段队列不可用，重新输入后按连续区间处理: {error}"
            )
            cls._close_composite_voice_panel(page, cancel_check=cancel_check)
            cls._clear_composite_queue(page, cancel_check=cancel_check)
            cls._input_composite_text(page, rows, cancel_check=cancel_check)
            marking_plan = cls._composite_marking_plan(rows)
            base_index = marking_plan["base_index"]
            correction_groups = marking_plan["correction_groups"]
            try:
                cls._select_editor_rows(
                    page, rows, 0, len(rows) - 1, cancel_check=cancel_check
                )
                cls._apply_composite_voice_to_selection(
                    page,
                    rows,
                    0,
                    len(rows) - 1,
                    config_row=rows[base_index],
                    cancel_check=cancel_check,
                )
            except XunfeiCancelled:
                raise
            except XunfeiError as fallback_error:
                _log(
                    f"[xunfei]   多人配音全文基准标注失败，按连续区间处理: {fallback_error}"
                )
                cls._close_composite_voice_panel(page, cancel_check=cancel_check)
                cls._input_composite_text(page, rows, cancel_check=cancel_check)
                for first_index, last_index in groups:
                    _check_cancel_requested(cancel_check)
                    cls._select_editor_rows(
                        page, rows, first_index, last_index,
                        cancel_check=cancel_check,
                    )
                    cls._apply_composite_voice_to_selection(
                        page, rows, first_index, last_index,
                        cancel_check=cancel_check,
                    )
            else:
                for first_index, last_index in correction_groups:
                    _check_cancel_requested(cancel_check)
                    cls._select_editor_rows(
                        page, rows, first_index, last_index,
                        cancel_check=cancel_check,
                    )
                    cls._apply_composite_voice_to_selection(
                        page, rows, first_index, last_index,
                        cancel_check=cancel_check,
                    )
            marking_mode = "连续区间回退"
            marking_group_count = len(correction_groups) + 1
        else:
            marking_mode = "多段队列"
            marking_group_count = len(queue_plan)

        marking_duration_ms = round((time.perf_counter() - started_at) * 1000)
        _log(
            f"[xunfei]   多人配音音色标注完成：模式={marking_mode}，"
            f"统一配置组 {marking_group_count} 组，耗时 {marking_duration_ms}ms"
        )

        pause_started_at = time.perf_counter()
        _check_cancel_requested(cancel_check)
        if boundaries and not cls._close_composite_voice_panel(
            page, cancel_check=cancel_check
        ):
            raise XunfeiError("多人配音音色面板未关闭，无法定位停顿工具栏")
        for row_index, boundary_ms in boundaries:
            _check_cancel_requested(cancel_check)
            cls._insert_composite_pause(
                page,
                row_index,
                boundary_ms,
                emit_log=False,
                verify=False,
                cancel_check=cancel_check,
            )
        if boundaries:
            all_pauses_inserted = _poll(
                lambda: (
                    True
                    if not cls._read_composite_pause_issues(page, boundaries)
                    else None
                ),
                timeout=3,
                interval=0.04,
                max_interval=0.2,
                page=page,
                cancel_check=cancel_check,
            )
            if not all_pauses_inserted:
                issues = cls._read_composite_pause_issues(page, boundaries)
                duplicate_rows = [item for item in issues if item.get("count", 0) > 1]
                if duplicate_rows:
                    raise XunfeiError(
                        "讯飞停顿插入校验失败：检测到重复停顿标记，"
                        f"行 {[item['row'] + 1 for item in duplicate_rows]}"
                )
                for item in issues:
                    _check_cancel_requested(cancel_check)
                    cls._insert_composite_pause(
                        page,
                        item["row"],
                        item["value"],
                        emit_log=False,
                        verify=True,
                        cancel_check=cancel_check,
                    )
                if cls._read_composite_pause_issues(page, boundaries):
                    raise XunfeiError("讯飞停顿批量插入后回读仍不完整")
            _log(
                f"[xunfei]   多人配音停顿已批量完成：{len(boundaries)} 处，"
                f"耗时 {round((time.perf_counter() - pause_started_at) * 1000)}ms，"
                "每处均按段落末尾 UI 定位并回读校验"
            )
        return rows, boundaries
