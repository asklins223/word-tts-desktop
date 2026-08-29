"""Page interaction actions for the legacy Xunfei session.

This mixin owns DOM interaction and confirmation dialogs.  It intentionally
keeps the existing method names so the compatibility session can migrate one
boundary at a time without changing callers.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from .config import (
    PARAM_DEFAULT,
    _MULTI_SELECT_MODIFIER,
    _SELECT_ALL,
    clamp_param,
)
from .errors import (
    XunfeiError,
    XunfeiSubmissionAmbiguous,
    _check_cancel_requested,
    _log,
)
from .helpers import poll as _poll, safe_eval as _safe_eval
from .page_scripts import AI_FLAG_KEYWORD_VARIANTS, JS
from .voice_catalog import DEFAULT_FEMALE, get_voice_info


def _probe_synth_state(page):
    """一次读取讯飞页面状态，避免同一轮重复执行多次 DOM 全量扫描。"""
    result = _safe_eval(page, JS.PROBE_SYNTH_STATE, AI_FLAG_KEYWORD_VARIANTS)
    return result if isinstance(result, dict) else None


class PageActionsMixin:

    # ------------------------------------------------------------------
    # 拟人行为辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _pause(page, base, spread=0.4):
        """拟人等待 base±spread 秒。"""
        seconds = max(0.05, base + ((time.time() * 7) % 1) * 2 * spread - spread)
        page.wait_for_timeout(int(seconds * 1000))

    @staticmethod
    def _type_text(page, text):
        """
        在已聚焦的编辑器中一次性插入文本，等价于用户粘贴。

        讯飞编辑器对长文本逐字符击键非常慢，也容易让页面在输入期间
        进入半更新状态；这里不再按字符调用 keyboard.type。若编辑器不
        接受键盘插入，再用 contenteditable 的 fill 做一次性兜底。
        """
        value = str(text or "")
        try:
            page.keyboard.insert_text(value)
            return True
        except Exception as exc:
            _log(f"[xunfei]   一次性插入文本失败，尝试编辑器填充: {exc}")
        try:
            page.locator(".ssml-editor").first.fill(value, timeout=5000)
            return True
        except Exception as exc:
            _log(f"[xunfei]   编辑器一次性填充失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # 页面基础操作
    # ------------------------------------------------------------------

    def _is_logged_in(self, page):
        """检测是否已登录：可见登录按钮消失 = 已登录。

        讯飞页面会把登录入口保留在隐藏菜单、模板节点或无障碍树中。
        扫描所有 ``button`` 会把这些不可见节点误判为“未登录”，导致
        浏览器已经打开后，后台无期限等待用户再次扫码。
        """
        try:
            if getattr(self, "_browser_disconnected", False) or page.is_closed():
                return False
            current_url = str(getattr(page, "url", "") or "").lower()
            if any(marker in current_url for marker in ("/login", "/signin", "/auth/")):
                return False
            btns = page.locator("button:visible, [role='button']:visible")
            for i in range(min(btns.count(), 50)):
                try:
                    txt = re.sub(r"\s+", "", btns.nth(i).inner_text()).strip()
                    if txt in {"登录", "立即登录", "扫码登录", "手机号登录", "登录注册"}:
                        return False
                except Exception:
                    pass
            dialogs = page.locator(
                ".ant-modal:visible, [role='dialog']:visible, .el-dialog:visible"
            )
            for i in range(min(dialogs.count(), 20)):
                try:
                    text = re.sub(r"\s+", "", dialogs.nth(i).inner_text()).strip()
                    if "登录" in text and any(token in text for token in ("扫码", "手机号", "验证码")):
                        return False
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _clear_editor(self, page):
        """清空文本编辑器内容。"""
        _safe_eval(page, JS.CLEAR_EDITOR)
        page.wait_for_timeout(200)
        actual = _safe_eval(page, JS.GET_EDITOR_TEXT)
        if actual:
            try:
                page.locator(".ssml-editor").first.click(timeout=3000)
                page.keyboard.press(_SELECT_ALL)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
            except Exception:
                pass

    def _input_text(self, page, text):
        """在编辑器中拟人输入文本并验证。"""
        self._clear_editor(page)
        page.locator(".ssml-editor").first.click(timeout=5000)
        self._pause(page, 0.15, 0.08)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        self._pause(page, 0.1, 0.05)
        self._type_text(page, text)
        page.wait_for_timeout(150)

        for attempt in range(2):
            actual = _safe_eval(page, JS.GET_EDITOR_TEXT) or ""
            if len(actual) >= len(text) * 0.85:
                return True
            _log(f"[xunfei]   输入验证失败 (attempt {attempt + 1})，重试...")
            self._clear_editor(page)
            page.locator(".ssml-editor").first.click(timeout=5000)
            self._type_text(page, text)
            page.wait_for_timeout(150)
        return False

    @staticmethod
    def _clear_editor_with_keyboard(page):
        """只用真实键盘操作清空编辑器，供多人配音 UI 流程使用。"""
        # 讯飞失败重试时可能还留着 ssml-float-bar；先用键盘收起它，
        # 避免真实 editor.click 被浮动条遮挡。
        page.keyboard.press("Escape")
        page.wait_for_timeout(30)
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        page.keyboard.press(_SELECT_ALL)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        paragraphs = page.locator(".ssml-editor p")
        remaining = []
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            text = paragraph.inner_text(timeout=1000)
            # ProseMirror 空编辑器会显示 contenteditable=false 的占位符，
            # 它属于 UI 提示而不是用户文本，不能把它误判成清空失败。
            placeholders = paragraph.locator(".ssml-editor-placeholder")
            for placeholder_index in range(placeholders.count()):
                placeholder_text = placeholders.nth(placeholder_index).inner_text(
                    timeout=500
                )
                text = text.replace(placeholder_text, "")
            text = text.strip()
            if text:
                remaining.append(text)
        if remaining:
            raise XunfeiError(
                "讯飞编辑器未能通过键盘清空，停止多人配音 UI 操作"
            )

    @classmethod
    def _read_editor_paragraphs(cls, page):
        """读取编辑器的可见段落文本，不修改页面。"""
        paragraphs = page.locator(".ssml-editor p")
        values = []
        for index in range(paragraphs.count()):
            paragraph = paragraphs.nth(index)
            text = paragraph.inner_text(timeout=1000)
            placeholders = paragraph.locator(".ssml-editor-placeholder")
            for placeholder_index in range(placeholders.count()):
                placeholder_text = placeholders.nth(placeholder_index).inner_text(
                    timeout=500
                )
                text = text.replace(placeholder_text, "")
            values.append(text)
        return values

    @classmethod
    def _input_composite_text(cls, page, rows):
        """把多人配音的逻辑行按真实编辑器段落输入并回读。"""
        values = [str(row.get("text") or "") for row in rows]
        if not values or any(not value.strip() for value in values):
            raise XunfeiError("多人配音 UI 文本包含空行，无法安全定位选区")
        cls._clear_editor_with_keyboard(page)
        editor = page.locator(".ssml-editor").first
        editor.click(timeout=5000)
        cls._type_text(page, "\n".join(values))
        page.wait_for_timeout(250)
        actual = cls._read_editor_paragraphs(page)
        if len(actual) != len(values):
            raise XunfeiError(
                "多人配音 UI 文本段落数量校验失败："
                f"期望 {len(values)}，实际 {len(actual)}"
            )
        for index, (expected, received) in enumerate(zip(values, actual)):
            if received.strip() != expected.strip():
                raise XunfeiError(
                    f"多人配音 UI 文本第 {index + 1} 行校验失败："
                    f"期望 {expected!r}，实际 {received!r}"
                )
        return True

    @staticmethod
    def _normalize_selection_text(value):
        return re.sub(r"\s+", "", str(value or ""))

    @classmethod
    def _verify_editor_selection(cls, page, expected_values):
        """校验当前浏览器选区恰好覆盖目标行，禁止误选全文。"""
        selected = _safe_eval(page, JS.GET_SELECTION_TEXT) or ""
        expected = "".join(str(value or "") for value in expected_values)
        if cls._normalize_selection_text(selected) != cls._normalize_selection_text(expected):
            raise XunfeiError(
                "多人配音 UI 选区校验失败："
                f"期望 {expected!r}，实际 {selected!r}；已停止以免误套用音色"
            )
        return selected

    @classmethod
    def _select_editor_rows(cls, page, rows, first_index, last_index):
        """通过真实页面选区选中一行或一段连续逻辑行。

        讯飞编辑器通常会把多行文本放进可滚动的 contenteditable 中。
        仅靠一次从首行拖到末行的鼠标动作，在长文档或打包客户端的小窗口
        中很容易因为滚动导致首尾不同时可见，进而误选或漏选。这里按真实
        浏览器交互的可靠性依次尝试 Shift-click、鼠标拖选，最后才用页面
        Range 重新建立同一个浏览器选区；三种方式都必须通过精确文本回读。
        任何方式都失败时直接停止，不能把一个本应批量设置的组拆成逐行操作。
        """
        if first_index < 0 or last_index < first_index or last_index >= len(rows):
            raise XunfeiError("多人配音 UI 选区索引越界")
        paragraphs = page.locator(".ssml-editor p")
        if paragraphs.count() != len(rows):
            raise XunfeiError(
                "多人配音 UI 选区前段落数量已变化，拒绝继续操作"
            )

        first = paragraphs.nth(first_index)
        last = paragraphs.nth(last_index)
        expected_values = [row["text"] for row in rows[first_index:last_index + 1]]
        if first_index == last_index:
            # Playwright 的 select_text 只选当前段落，绝不退化为编辑器全选。
            first.select_text(timeout=5000)
            page.wait_for_timeout(80)
            return cls._verify_editor_selection(page, expected_values)

        errors = []

        def paragraph_text_target(paragraph):
            # 讯飞完成一次音色标记后，段落开头会多出一个不可编辑的
            # speaker 标签。直接对整个 <p> 执行 select_text() 会把这个
            # 标签当成选区起点，页面有时会因此把后续音色套用到错误范围。
            # 优先只选真正可编辑的正文 span；未标注段落仍使用 <p> 本身。
            content = paragraph.locator(
                'span.range-annotation-content.speaker-content'
                ':not(.ssml-tag):not([data-type="range_anchor"]):visible'
            )
            try:
                if content.count() == 1:
                    return content.first
            except Exception:
                pass
            return paragraph

        # 方式一：先真实选中首行，再滚动到末行并 Shift-click。这个动作
        # 不要求首尾同时出现在视口中，最适合打包客户端的窄窗口和长文档。
        try:
            first_target = paragraph_text_target(first)
            last_target = paragraph_text_target(last)
            first_target.scroll_into_view_if_needed(timeout=5000)
            first_target.select_text(timeout=5000)
            last_target.scroll_into_view_if_needed(timeout=5000)
            last_box = last_target.bounding_box()
            if not last_box:
                raise XunfeiError("末行不可见，无法执行 Shift-click")
            last_target.click(
                position={
                    "x": max(2, last_box["width"] - 2),
                    "y": max(2, last_box["height"] - 2),
                },
                modifiers=["Shift"],
                timeout=5000,
            )
            page.wait_for_timeout(120)
            selected = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（Shift-click）"
            )
            return selected
        except Exception as error:
            errors.append(f"Shift-click: {error}")

        # 方式二：短范围仍优先使用真实鼠标拖选，兼容讯飞页面没有稳定
        # 锚点行为的版本。只有首尾都在当前视口时才执行，避免跨滚动拖选。
        try:
            first_target = paragraph_text_target(first)
            last_target = paragraph_text_target(last)
            first_target.scroll_into_view_if_needed(timeout=5000)
            last_target.scroll_into_view_if_needed(timeout=5000)
            first_box = first_target.bounding_box()
            last_box = last_target.bounding_box()
            if not first_box or not last_box:
                raise XunfeiError("首尾行不可同时看见，无法执行鼠标拖选")
            start = {
                "x": first_box["x"] + 2,
                # 从首段第一行附近开始，长句换行时不能从段落中间起拖。
                "y": first_box["y"] + 2,
            }
            end = {
                "x": max(last_box["x"] + 2, last_box["x"] + last_box["width"] - 2),
                # 到末段最后一行附近结束，避免漏选长句的尾音文本。
                "y": max(last_box["y"] + 2, last_box["y"] + last_box["height"] - 2),
            }
            page.mouse.move(start["x"], start["y"])
            page.mouse.down()
            page.mouse.move(end["x"], end["y"], steps=8)
            page.mouse.up()
            page.wait_for_timeout(120)
            selected = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（鼠标拖选）"
            )
            return selected
        except Exception as error:
            errors.append(f"鼠标拖选: {error}")

        # 方式三：仍然只改变浏览器当前 Selection，不调用讯飞接口，也不
        # 修改编辑器内容。它是跨滚动场景的页面交互兜底，后续“使用”按钮
        # 仍由页面 UI 读取这个选区并产生 speaker 标记。
        try:
            selected = _safe_eval(
                page,
                JS.SELECT_EDITOR_RANGE,
                [first_index, last_index],
            )
            page.wait_for_timeout(120)
            verified = cls._verify_editor_selection(page, expected_values)
            _log(
                f"[xunfei]   多人配音批量选区行 {first_index + 1}-"
                f"{last_index + 1}（页面选区兜底）"
            )
            return verified
        except Exception as error:
            errors.append(f"页面选区兜底: {error}")

        detail = "；".join(str(error) for error in errors[-3:])
        raise XunfeiError(
            f"多人配音 UI 批量选区失败：行 {first_index + 1}-{last_index + 1}；{detail}"
        )

    @classmethod
    def _read_composite_queue_count(cls, page):
        """读取讯飞页面多段选择队列数量。

        页面在编辑器滚动后可能暂时不渲染浮动的 ``已选 N 段`` 徽标，
        但仍会保留每个选区对应的 ``.msq-pending-range`` 装饰节点。
        徽标和装饰节点都属于网页 UI 状态，后者作为同一页面交互的回读
        兜底，避免长文档被误判为空队列。
        """
        try:
            # 浮动工具条在滚动期间可能短暂隐藏，但队列状态仍然保留；
            # 读取隐藏条的文本比把短暂不可见误判成队列已清空更安全。
            pending = page.locator(".msq-pending-range")
            pending_count = pending.count()
            if pending_count > 0:
                # 连续范围的一次拖选在徽标中计为 1 个队列区间，但页面
                # 装饰节点会按实际段落各保留一个；这里校验段落覆盖数，
                # 才能确认没有漏掉连续范围中的第二行。
                return pending_count

            badge = page.locator(".msq-queue-badge")
            for index in range(badge.count()):
                text = badge.nth(index).inner_text(timeout=1000)
                match = re.search(r"已选\s*(\d+)\s*段", text or "")
                if match:
                    return int(match.group(1))
            return 0
        except Exception:
            return 0

    @classmethod
    def _clear_composite_queue(cls, page):
        """清空讯飞网页的多段选区队列，不改动编辑器文本。"""
        try:
            # 选区浮动条本身会拦截 editor.click。先用真实键盘 Escape
            # 收起工具条并清掉队列，只有页面仍保留待处理段落时才需要
            # 再点击编辑器确认焦点。
            page.keyboard.press("Escape")
            if cls._read_composite_queue_count(page) == 0:
                return True
            page.wait_for_timeout(20)
            if cls._read_composite_queue_count(page) == 0:
                return True
            editor = page.locator(".ssml-editor").first
            editor.click(timeout=3000)
            page.keyboard.press("Escape")
        except Exception:
            return False
        return bool(_poll(
            lambda: cls._read_composite_queue_count(page) == 0,
            timeout=3,
            interval=0.1,
            page=page,
        ))

    @classmethod
    def _select_composite_queue_rows(cls, page, rows, ranges, *, native=False):
        """用讯飞网页真实的 Command/Ctrl 多选队列加入多个不连续区间。

        讯飞的多段队列只在真实 pointerup 带有 Command/Ctrl 修饰键时生效，
        不能用一次全选替代。因此正常路径先在当前真实页面中用 Range 精确
        建立一行正文选区，再用带修饰键的真实鼠标 pointerup 加入队列；这
        比 Playwright 对每行执行 select_text 少一次编辑器节点往返。最终
        “使用”动作仍只执行一次。Range 路径只负责建立浏览器当前选区，若
        页面版本没有正确接受它，调用方会清空队列并切回原生 select_text。
        """
        normalized_ranges = [
            (int(first), int(last))
            for first, last in ranges
            if int(first) <= int(last)
        ]
        if not normalized_ranges:
            raise XunfeiError("多人配音多段选区没有可加入的目标区间")
        if cls._read_composite_queue_count(page) != 0:
            raise XunfeiError("多人配音多段选区开始前仍有上一组待处理选区")
        if any(
            first < 0 or last >= len(rows)
            for first, last in normalized_ranges
        ):
            raise XunfeiError("多人配音多段选区索引越界")

        paragraphs = page.locator(".ssml-editor p")
        if paragraphs.count() != len(rows):
            raise XunfeiError(
                "多人配音多段选区前段落数量已变化，拒绝继续操作"
            )

        def paragraph_text_target(paragraph):
            # 这里每次都会先由 _input_composite_text 清空编辑器，待标注
            # 的目标段落不含 speaker 标签；直接操作 <p> 可省掉每行一次
            # 子节点计数往返。最终套用音色后仍用整组 DOM 回读校验正文。
            return paragraph

        def select_exact_text(row_index):
            """用页面 Range 或浏览器原生方式选中一整行正文。"""
            target = paragraph_text_target(paragraphs.nth(row_index))
            if not native:
                selected = _safe_eval(
                    page,
                    JS.SELECT_EDITOR_ROW,
                    row_index,
                )
                if not isinstance(selected, dict):
                    raise XunfeiError(
                        f"多人配音快速选区失败：第 {row_index + 1} 行不可见"
                    )
                expected_text = rows[row_index].get("text") or ""
                if cls._normalize_selection_text(selected.get("text")) != cls._normalize_selection_text(expected_text):
                    raise XunfeiError(
                        f"多人配音快速选区回读失败：第 {row_index + 1} 行正文不一致"
                    )
                box = selected.get("box")
                if not isinstance(box, dict):
                    raise XunfeiError(
                        f"多人配音快速选区失败：第 {row_index + 1} 行坐标不可用"
                    )
                page.wait_for_timeout(20)
                return None, box

            # 保留一次轻量居中滚动：讯飞的待处理选区装饰只在目标行进入
            # 当前编辑器视口后才稳定回读。等待从旧实现的 120ms 降到 30ms，
            # 仍避免长文档滚动尚未完成就发送 pointerup。
            try:
                box = target.evaluate(
                    """el => {
                        el.scrollIntoView({
                        block: 'center',
                        inline: 'nearest',
                        behavior: 'instant',
                        });
                        const rect = el.getBoundingClientRect();
                        return {
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height,
                        };
                    }"""
                )
            except Exception:
                target.scroll_into_view_if_needed(timeout=5000)
                box = target.bounding_box()
            page.wait_for_timeout(20)
            target.select_text(timeout=5000)
            page.wait_for_timeout(20)
            return target, box

        def enqueue_current_selection(target, box):
            """用真实 Command/Ctrl pointerup 把当前 Selection 加入队列。

            讯飞队列监听的是 pointerup，而不是某个内部接口。先由浏览器
            原生 select_text/Shift-click 完整选区，再只发送一次带修饰键的
            真实鼠标 pointerup，避免长句换行时依赖鼠标拖动终点。
            """
            if not box or box["width"] < 4 or box["height"] < 4:
                raise XunfeiError("多人配音多段选区目标行不可见")
            def send_pointerup():
                page.keyboard.down(_MULTI_SELECT_MODIFIER)
                try:
                    # 长段落可能占两三行。讯飞的 pointerup 监听在已有队列
                    # 遮罩出现后，对段落中间/末行的坐标并不总会触发；首行
                    # 的正文区域在滚动和浮动工具条出现后仍稳定可用。
                    y = box["y"] + min(6, max(3, box["height"] * 0.12))
                    page.mouse.move(
                        box["x"] + box["width"] / 2,
                        y,
                    )
                    page.mouse.up()
                finally:
                    page.keyboard.up(_MULTI_SELECT_MODIFIER)

            send_pointerup()
            # 不逐行轮询装饰节点：讯飞会把选区装饰异步批量渲染，逐行等
            # 反而会在打包客户端里累积数百毫秒。固定给事件 20ms 落地，
            # 最终统一用 expected_count 回读；总数不符时由上层清空队列
            # 后重试整组，避免以速度换取漏段。
            page.wait_for_timeout(20)

        # 每行加入同一队列；连续配置仍由上层合并为一个配置组，后续只
        # 点击一次“使用”，不会退化成逐段打开音色面板。
        for first, last in normalized_ranges:
            for row_index in range(first, last + 1):
                target, box = select_exact_text(row_index)
                enqueue_current_selection(target, box)

        # 队列装饰按实际段落保留一个节点，徽标则可能按连续区间计数；
        # 这里校验段落覆盖总数，避免漏掉任一目标行。
        expected_count = sum(
            last - first + 1 for first, last in normalized_ranges
        )
        def expected_queue_count():
            current = cls._read_composite_queue_count(page)
            return current if current == expected_count else None

        actual_count = _poll(
            expected_queue_count,
            timeout=3,
            interval=0.1,
            max_interval=0.4,
            page=page,
        )
        if actual_count != expected_count:
            cls._clear_composite_queue(page)
            raise XunfeiError(
                "多人配音多段选区数量校验失败："
                f"期望 {expected_count} 个待选段，实际 {actual_count} 个"
            )
        _log(
            f"[xunfei]   多人配音已加入多段选区："
            f"{len(normalized_ranges)} 个配置区间、{expected_count} 行"
        )
        return actual_count

    def _select_voice(self, page, voice_name, voice_key=None):
        """搜索并选择指定发音人，并以页面实际选中态校验缓存。"""
        target_key = str(voice_key or "").strip() or None

        # 提交作品后讯飞页面可能把发音人恢复为平台默认值。不能只相信
        # 本地缓存，否则下一条同音色任务会跳过搜索，最终悄悄使用默认音色。
        # 同时按 key 追踪，避免音色目录出现同名发音人时错误复用。
        cache_matches = (
            self._current_voice_key == target_key
            if target_key is not None
            else self._current_voice_name == voice_name
        )
        if cache_matches:
            selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
            if selected:
                return True
            _log(
                f"[xunfei]   页面当前音色不是缓存的 '{voice_name}'，"
                "强制重新搜索"
            )
            self._current_voice_key = None
            self._current_voice_name = None

        _log(
            f"[xunfei]   搜索并选择发音人: {voice_name}"
            + (f" (key={target_key})" if target_key else "")
        )

        def mark_selected():
            # 讯飞页面切换音色后会把三项调节恢复为页面默认值；即使新旧
            # 音色的目标数值恰好相同，也必须让 _apply_params() 重新下发。
            voice_changed = (
                self._current_voice_key != target_key
                if target_key is not None
                else self._current_voice_name != voice_name
            )
            self._current_voice_key = target_key
            self._current_voice_name = voice_name
            if voice_changed:
                self._applied_params = None
            return True

        for round_idx in range(2):
            selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
            if selected:
                return mark_selected()

            search_input = page.locator(
                "input.h-full.w-full, input[placeholder*='搜索'], input[placeholder*='音色'], input[placeholder*='主播']"
            )
            if search_input.count() > 0:
                search_input.first.click(timeout=3000)
                search_input.first.fill("")
                self._pause(page, 0.15, 0.06)
                search_input.first.fill(voice_name)
                _poll(
                    lambda: _safe_eval(page, JS.CHECK_SEARCH_RESULT, voice_name),
                    timeout=5, interval=0.6, page=page,
                )

            clicked = _safe_eval(page, JS.SEARCH_AND_CLICK_VOICE, voice_name)
            if clicked:
                self._pause(page, 0.6, 0.25)
                selected = _safe_eval(page, JS.CHECK_VOICE_SELECTED, voice_name)
                if selected:
                    return mark_selected()
                _log(f"[xunfei]   发音人 '{voice_name}' 点击后未见选中态，重试...")

        raise XunfeiError(f"未找到或无法选中发音人: {voice_name}")

    def _apply_params(self, page, speed, pitch, volume):
        """
        设置语速/语调/音量三项并回读验证。
        与已应用参数一致时跳过；切换发音人后必须重新应用（站点会重置参数）。
        """
        targets = {"speed": clamp_param(speed), "pitch": clamp_param(pitch),
                   "volume": clamp_param(volume)}
        if self._applied_params == targets:
            return True

        labels = ("语速", "语调", "音量")
        values = (targets["speed"], targets["pitch"], targets["volume"])
        failed_labels = []
        for idx, (label, value) in enumerate(zip(labels, values)):
            ok = False
            # 方式一：真实键盘输入（点击 → 全选 → 输入 → Tab 失焦）
            try:
                loc = page.locator("input.w-12").nth(idx)
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    page.keyboard.press(_SELECT_ALL)
                    page.keyboard.type(str(value))
                    page.keyboard.press("Tab")
                    self._pause(page, 0.25, 0.1)
                    readback = _safe_eval(page, JS.READ_PARAM_INPUTS) or []
                    ok = idx < len(readback) and readback[idx].strip() == str(value)
            except Exception:
                ok = False
            # 方式二：JS 注入兜底 + 回读验证
            if not ok:
                _safe_eval(page, JS.SET_PARAM_INPUT, [idx, value])
                self._pause(page, 0.2, 0.08)
                readback = _safe_eval(page, JS.READ_PARAM_INPUTS) or []
                ok = idx < len(readback) and readback[idx].strip() == str(value)
            if not ok:
                _log(f"[xunfei]   ⚠️ 参数[{label}] 设置为 {value} 后回读不一致")
                failed_labels.append(label)

        if failed_labels:
            # 不能把未验证成功的参数写入缓存，否则后续合成会跳过设置，
            # 最终生成的音频可能悄悄使用了网页上的旧参数。
            self._applied_params = None
            failed = ", ".join(failed_labels)
            raise XunfeiError(f"讯飞参数设置失败，回读不一致: {failed}")

        self._applied_params = dict(targets)
        applied_log = ", ".join(f"{l}={v}" for l, v in zip(labels, values))
        _log(f"[xunfei]   参数已应用: {applied_log}")
        return True

    def _click_generate(self, page):
        """点击'生成音频'按钮。"""
        btn = page.locator("button", has_text="生成音频")
        if btn.count() == 0:
            btn = page.locator("button.bg-blue-600")
        if btn.count() == 0:
            raise XunfeiError("未找到'生成音频'按钮")
        btn.first.click(timeout=5000)
        _log("[xunfei]   已点击生成音频")
        self._pause(page, 0.6, 0.3)

    @staticmethod
    def _normalize_works_name(value):
        """收敛讯飞作品名称，避免下载页名称被截断或包含非法字符。"""
        text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(value or "")).strip()
        return text[:25] or f"wordtts_{uuid.uuid4().hex[:10]}"

    def _set_works_name(self, page, works_name):
        """在作品设置弹窗中写入唯一名称，便于下载页人工核对。"""
        normalized = self._normalize_works_name(works_name)
        try:
            field = page.locator('input[placeholder*="作品名称"]:visible').first
            if field.count() == 0:
                return False
            field.click(timeout=3000)
            page.keyboard.press(_SELECT_ALL)
            page.keyboard.insert_text(normalized)
            page.keyboard.press("Tab")
            self._pause(page, 0.2, 0.08)
            actual = field.input_value(timeout=1000)
            if actual == normalized:
                _log(f"[xunfei]   作品名称已设置: {normalized}")
                return True
        except Exception as error:
            _log(f"[xunfei]   作品名称设置失败（继续使用默认名称）: {error}")
        return False

    @classmethod
    def _set_mp3_format_with_locator(cls, page):
        """用 Playwright locator 兜底选择作品设置中的 MP3 单选项。

        讯飞的实际 DOM 没有稳定的 MP3 value，选项文本在 ``label`` 内；
        因此这里同时读取 input value 和 label 文本，但永远不会按“第一个
        单选项”点击，避免 MP3 缺失时误选 WAV。
        """
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                radios = dialog.locator(
                    'input[type="radio"][name="exportFormat"]'
                )
                if "作品设置" not in text or radios.count() == 0:
                    continue

                mp3 = None
                mp3_label = None
                for radio_index in range(radios.count()):
                    radio = radios.nth(radio_index)
                    value = (radio.get_attribute("value") or "").strip().lower()
                    label = radio.locator("xpath=ancestor::label[1]")
                    try:
                        label_text = re.sub(r"\s+", "", label.inner_text(timeout=500)).lower()
                    except Exception:
                        try:
                            label_text = re.sub(
                                r"\s+", "", radio.evaluate(
                                    "element => element.parentElement?.textContent || ''"
                                )
                            ).lower()
                        except Exception:
                            label_text = ""
                    if (
                        value == "mp3"
                        or label_text == "mp3"
                        or label_text.startswith("mp3")
                    ):
                        mp3 = radio
                        mp3_label = label
                        break

                if mp3 is None:
                    return "mp3_not_found"
                if mp3.is_checked():
                    return "already_locator"
                if mp3.is_disabled():
                    return "mp3_disabled"

                mp3.click(force=True, timeout=2000)
                if mp3.is_checked():
                    return "clicked_locator"
                if mp3_label is not None and mp3_label.count() > 0:
                    mp3_label.click(force=True, timeout=2000)
                return "clicked_locator"
        except Exception as error:
            _log(f"[xunfei]   locator 选择 MP3 失败: {error}")
        return None

    @classmethod
    def _read_mp3_format_with_locator(cls, page):
        """读取 locator 看到的作品设置格式，仅返回 MP3 的真实勾选状态。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                radios = dialog.locator(
                    'input[type="radio"][name="exportFormat"]'
                )
                if "作品设置" not in text or radios.count() == 0:
                    continue
                for radio_index in range(radios.count()):
                    radio = radios.nth(radio_index)
                    value = (radio.get_attribute("value") or "").strip().lower()
                    label = radio.locator("xpath=ancestor::label[1]")
                    try:
                        label_text = re.sub(r"\s+", "", label.inner_text(timeout=500)).lower()
                    except Exception:
                        label_text = ""
                    if (
                        value == "mp3"
                        or label_text == "mp3"
                        or label_text.startswith("mp3")
                    ):
                        return "mp3" if radio.is_checked() else "other"
                return "mp3_not_found"
        except Exception:
            pass
        return None

    def _ensure_mp3_format(self, page, timeout=10, cancel_check=None):
        """在最终确认合成前强制确认讯飞作品格式为 MP3。

        这里不接受“默认应该是 MP3”作为成功条件：必须找到真实的
        ``exportFormat`` MP3 radio，并在点击后回读 checked 状态；否则不
        点击“确认合成”，防止在 Windows/不同账号默认值为 WAV 时生成失败。
        """
        def set_probe():
            result = _safe_eval(page, JS.SET_MP3_FORMAT)
            if isinstance(result, dict) and result.get("status") != "not_found":
                return result
            return None

        result = _poll(
            set_probe,
            timeout=timeout,
            interval=0.35,
            page=page,
            cancel_check=cancel_check,
        )
        status = result.get("status") if isinstance(result, dict) else None
        if status not in {"already_mp3", "clicked_mp3"}:
            # JS 选择器失败时只按同一套精确规则兜底，绝不退化为 first radio。
            fallback = self._set_mp3_format_with_locator(page)
            if fallback in {"already_locator", "clicked_locator"}:
                status = fallback
            elif fallback in {"mp3_not_found", "mp3_disabled"}:
                status = fallback

        if status in {"mp3_not_found", "mp3_disabled"}:
            _log(
                "[xunfei]   作品设置中没有可用的 MP3 选项，"
                f"停止提交 (status={status})"
            )
            return False

        def read_probe():
            state = _safe_eval(page, JS.GET_MP3_FORMAT)
            if isinstance(state, dict) and state.get("status") != "not_found":
                return state
            return None

        state = _poll(
            read_probe,
            timeout=4,
            interval=0.25,
            page=page,
            cancel_check=cancel_check,
        )
        if not isinstance(state, dict) or not state.get("checked"):
            # React 受控单选项偶尔会让 JS click 后的 DOM 更新稍慢；只有在
            # 回读仍未确认时才使用 locator，再次点击同一个 MP3 选项。
            fallback = self._set_mp3_format_with_locator(page)
            if fallback in {"already_locator", "clicked_locator"}:
                state = _poll(
                    read_probe,
                    timeout=3,
                    interval=0.25,
                    page=page,
                    cancel_check=cancel_check,
                )

        if isinstance(state, dict) and state.get("checked"):
            _log(
                "[xunfei]   作品设置格式已确认为 MP3 "
                f"(status={status or state.get('status')})"
            )
            return True

        # 最后再读取一次 locator 状态，日志里明确区分“没弹窗”和“MP3
        # 不存在/未勾选”，便于定位 Windows 端页面结构差异。
        locator_state = self._read_mp3_format_with_locator(page)
        if locator_state == "mp3":
            _log("[xunfei]   作品设置格式已确认为 MP3 (locator)")
            return True
        snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
        if snapshot:
            _log(
                "[xunfei]   作品设置 MP3 格式确认失败，当前弹窗: "
                + json.dumps(snapshot, ensure_ascii=False)[:1800]
            )
        else:
            _log(
                "[xunfei]   作品设置 MP3 格式确认失败: "
                f"status={status or 'not_found'}, locator={locator_state or 'not_found'}"
            )
        return False

    @staticmethod
    def _visible_confirm_synth_buttons(page):
        """返回当前页面可见的“确认合成”按钮，兼容讯飞弹窗 DOM 变化。"""
        buttons = []
        try:
            candidates = page.locator('button:visible')
            for index in range(min(candidates.count(), 200)):
                button = candidates.nth(index)
                try:
                    label = re.sub(r"\s+", "", button.inner_text(timeout=500)).strip()
                except Exception:
                    continue
                if label != "确认合成":
                    continue
                try:
                    disabled = button.is_disabled()
                except Exception:
                    disabled = None
                buttons.append((button, disabled))
        except Exception:
            pass
        return buttons

    @classmethod
    def _click_confirm_synth_button(cls, page):
        """用可见按钮 locator 点击“确认合成”，并记录现场状态。"""
        buttons = cls._visible_confirm_synth_buttons(page)
        if buttons:
            _log(
                "[xunfei]   确认合成按钮现场: "
                + ", ".join(f"visible disabled={disabled}" for _, disabled in buttons)
            )
        for button, disabled in buttons:
            if disabled is True:
                continue
            try:
                button.click(force=True, timeout=3000)
                return True
            except Exception as error:
                _log(f"[xunfei]   locator 点击确认合成失败: {error}")
        return False

    # ------------------------------------------------------------------
    # 确认合成弹窗流程
    # ------------------------------------------------------------------

    def _observe_after_first_confirm(self, page, cancel_check=None):
        """第一次点击确认合成后的状态探测。"""

        def probe():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            # 第一次确认后，确认按钮本身可能还没卸载；这里只接受真正的
            # AI/错误/订单状态，避免把旧的确认弹窗当成已完成。
            return state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order",
            } else None

        result = _poll(
            probe,
            # 讯飞页面的 React 弹层可能在点击后数秒才挂载；保留较长
            # 的等待窗口，但每轮只做一次合并状态快照，避免拖慢浏览器。
            timeout=15,
            interval=0.4,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not result:
            _check_cancel_requested(cancel_check)
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            result = state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order",
            } else None
        result = result or "none"
        if result == "none":
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   第一次确认后未检测到 AI 标识弹窗，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            else:
                _log("[xunfei]   第一次确认后未检测到 AI 标识弹窗（可能已选择‘不再提示’，或讯飞本次未展示）")
        return result

    @staticmethod
    def _find_visible_dialog(page, text_fragment):
        """按文案找到可见弹窗，兼容 Ant Design 和新版通用 dialog。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                try:
                    text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                except Exception:
                    continue
                if text_fragment in text:
                    return dialog
        except Exception:
            pass
        return None

    @classmethod
    def _click_no_remind_with_locator(cls, page):
        """JS 找不到复选框时，用 Playwright 强制点击真实控件兜底。"""
        dialog = cls._find_visible_dialog(page, "不再提示")
        if dialog is None:
            return None
        try:
            inputs = dialog.locator('input[type="checkbox"], .ant-checkbox-input')
            unchecked = []
            for index in range(inputs.count()):
                checkbox = inputs.nth(index)
                try:
                    if checkbox.is_checked():
                        continue
                except Exception:
                    continue
                unchecked.append(checkbox)
            if unchecked:
                unchecked[0].click(force=True, timeout=2000)
                return "clicked_locator_input"
            if inputs.count() > 0:
                return "already_locator"

            labels = dialog.locator('.ant-checkbox-wrapper, label, [role="checkbox"], button')
            for index in range(labels.count()):
                label = labels.nth(index)
                label_text = re.sub(r"\s+", "", label.inner_text(timeout=500))
                if "不再提示" not in label_text:
                    continue
                label.click(force=True, timeout=2000)
                return "clicked_locator_label"
        except Exception:
            pass
        return None

    @classmethod
    def _click_ai_switch_with_locator(cls, page):
        """用 locator 兜底点击确认合成弹窗中的 AI 标识开关。"""
        try:
            dialogs = page.locator(
                '.ant-modal:visible, .ant-modal-content:visible, [role="dialog"]:visible, '
                '.el-dialog:visible, .el-message-box:visible'
            )
            for index in range(min(dialogs.count(), 20)):
                dialog = dialogs.nth(index)
                text = re.sub(r"\s+", "", dialog.inner_text(timeout=500))
                if "不再提示" in text:
                    continue
                if not any(marker in text for marker in ("作品设置", "确认合成", "作品名称")):
                    continue
                switches = dialog.locator(
                    'button[role="switch"], [role="switch"], .ant-switch, button[aria-pressed]'
                )
                for switch_index in range(switches.count()):
                    switch = switches.nth(switch_index)
                    aria_checked = switch.get_attribute("aria-checked")
                    aria_pressed = switch.get_attribute("aria-pressed")
                    class_name = switch.get_attribute("class") or ""
                    is_on = (
                        aria_checked == "true"
                        or aria_pressed == "true"
                        or "ant-switch-checked" in class_name
                    )
                    if not is_on:
                        return "already_locator"
                    # 优先点击真实 button[role=switch]，让 Ant Design/React
                    # 收到完整的开关事件；不要只点击内部装饰 handle。
                    switch.click(force=True, timeout=2000)
                    return "clicked_locator"
        except Exception:
            pass
        return None

    def _ensure_ai_switch_off(self, page, timeout=12, cancel_check=None):
        """确保作品设置中的 AI 标识开关为关闭状态。

        讯飞有时跳过“AI 标识说明”弹窗，直接展示“作品设置”；因此这个
        检查必须独立于说明弹窗流程，并且必须回读 aria-checked/class 状态。
        返回 ``off``、``on`` 或 ``not_found``。
        """
        last_state = "not_found"
        js_click_attempted = False
        last_locator_attempt = 0.0

        def probe():
            nonlocal last_state, js_click_attempted, last_locator_attempt
            info = _probe_synth_state(page)
            if info and info.get("ai_modal"):
                # 说明弹窗可以延迟挂载；处理成功后从头回读作品设置，
                # 不把“当前还没看到 switch”误判为关闭成功。
                if self._handle_ai_flag_dialog(
                    page,
                    ensure_switch=False,
                    cancel_check=cancel_check,
                ):
                    js_click_attempted = False
                    last_locator_attempt = 0.0
                return None

            state = str((info or {}).get("ai_switch") or "not_found")
            last_state = state
            if state == "off":
                return "off"
            if state == "on":
                if not js_click_attempted:
                    clicked = _safe_eval(page, JS.CLICK_AI_SWITCH)
                    if clicked == "already_off":
                        return None
                    if clicked == "clicked":
                        js_click_attempted = True
                        self._pause(page, 0.18, 0.05)
                        return None
                # JS click 没有让 React 受控状态变化时，降低频率再用
                # locator 点击真实 button[role=switch]，避免连续点同一开关。
                now = time.monotonic()
                if now - last_locator_attempt >= 0.65:
                    last_locator_attempt = now
                    if self._click_ai_switch_with_locator(page):
                        self._pause(page, 0.25, 0.08)
                return None

            # switch 尚未挂载时也给 locator 一次机会；页面继续异步渲染时，
            # 自适应轮询会再次回到这里，不会漏掉延迟出现的开关。
            now = time.monotonic()
            if now - last_locator_attempt >= 0.65:
                last_locator_attempt = now
                if self._click_ai_switch_with_locator(page):
                    self._pause(page, 0.25, 0.08)
            return None

        result = _poll(
            probe,
            timeout=timeout,
            interval=0.2,
            max_interval=0.85,
            page=page,
            cancel_check=cancel_check,
        )
        if result == "off":
            return "off"
        return last_state

    @classmethod
    def _click_ai_confirm_with_locator(cls, page):
        """用 locator 兜底点击 AI 标识弹窗的确认按钮。"""
        dialog = cls._find_visible_dialog(page, "不再提示")
        if dialog is None:
            return False
        try:
            buttons = dialog.locator('button, [role="button"], .ant-btn')
            labels = {"确认", "确定", "知道了", "我知道了", "继续"}
            for index in range(buttons.count()):
                button = buttons.nth(index)
                label = re.sub(r"\s+", "", button.inner_text(timeout=500)).strip()
                if label not in labels:
                    continue
                button.click(force=True, timeout=2000)
                return True
        except Exception:
            pass
        return False

    def _handle_ai_flag_dialog(self, page, ensure_switch=True, cancel_check=None):
        def check_no_remind():
            result = _safe_eval(page, JS.CHECK_NO_REMIND)
            return result if result in {"clicked", "clicked_input", "clicked_label", "already"} else None

        checked = _poll(
            check_no_remind,
            timeout=10,
            interval=0.25,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not checked:
            checked = self._click_no_remind_with_locator(page)
        _log(f"[xunfei]   AI 标识弹窗‘不再提示’: {'✓' if checked else '✗'}{f' ({checked})' if checked else ''}")
        if not checked:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 弹窗未勾选‘不再提示’，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.35, 0.15)

        if ensure_switch:
            switch_state = self._ensure_ai_switch_off(
                page,
                timeout=12,
                cancel_check=cancel_check,
            )
            _log(
                f"[xunfei]   AI 标识开关关闭: "
                f"{'✓' if switch_state == 'off' else '未出现' if switch_state == 'not_found' else '✗'}"
                f" ({switch_state})"
            )
            if switch_state == "on":
                snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
                if snapshot:
                    _log(f"[xunfei]   AI 标识开关未确认关闭，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
                return False
            self._pause(page, 0.35, 0.15)

        confirmed = bool(_poll(
            lambda: _safe_eval(page, JS.CLICK_AI_CONFIRM),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        ))
        if not confirmed:
            confirmed = self._click_ai_confirm_with_locator(page)
        if not confirmed:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 弹窗仍未关闭，当前弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
        _log(f"[xunfei]   AI 标识弹窗确认: {'✓' if confirmed else '✗'}")
        if not confirmed:
            return False

        def ai_modal_closed():
            info = _probe_synth_state(page)
            return bool(info and info.get("ai_modal") is False)

        closed = _poll(
            ai_modal_closed,
            timeout=8,
            interval=0.25,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if not closed:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   AI 标识确认后弹窗仍存在: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return False
        self._pause(page, 0.5, 0.2)
        return True

    def _wait_order_or_error(self, page, timeout, cancel_check=None):
        def probe():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            if state == "order":
                return "ok"
            return state if state in {"insufficient", "rate_limited", "login"} else None

        result = _poll(
            probe,
            timeout=timeout,
            interval=0.8,
            max_interval=1.5,
            page=page,
            cancel_check=cancel_check,
        )
        if result:
            return result
        # 超时边界再做一次同步快照，覆盖最后一刻才挂载的错误/订单提示。
        info = _probe_synth_state(page)
        state = (info or {}).get("state")
        if state == "order":
            return "ok"
        return state if state in {"insufficient", "rate_limited", "login"} else None

    def _confirm_synth(self, page, works_name=None, cancel_check=None):
        """
        处理确认合成弹窗完整流程。

        返回: 'ok' | 'insufficient' | 'rate_limited' | 'login' | 'failed'
        """
        initial_ai_state = None
        self._confirm_click_succeeded = False
        self._submission_state_uncertain = False
        confirm_clicked = False

        def uncertain_after_confirm(reason):
            """确认按钮已点击后无法判定结果时，禁止回到通用重试。"""
            if confirm_clicked:
                raise XunfeiSubmissionAmbiguous(reason, works_name=works_name)
            return "failed"

        def ensure_ai_off(timeout=12):
            kwargs = {"timeout": timeout}
            if cancel_check is not None:
                kwargs["cancel_check"] = cancel_check
            return self._ensure_ai_switch_off(page, **kwargs)

        def ensure_mp3():
            if cancel_check is None:
                return self._ensure_mp3_format(page)
            return self._ensure_mp3_format(page, cancel_check=cancel_check)

        def observe_after_first_confirm():
            if cancel_check is None:
                return self._observe_after_first_confirm(page)
            return self._observe_after_first_confirm(
                page,
                cancel_check=cancel_check,
            )

        def handle_ai_flag(ensure_switch=False):
            kwargs = {"ensure_switch": ensure_switch}
            if cancel_check is not None:
                kwargs["cancel_check"] = cancel_check
            return self._handle_ai_flag_dialog(page, **kwargs)

        def wait_order(timeout):
            if cancel_check is None:
                return self._wait_order_or_error(page, timeout)
            return self._wait_order_or_error(
                page,
                timeout,
                cancel_check=cancel_check,
            )

        def ensure_ai_setting(allow_missing=False):
            # “订单支付”/“去下载”弹窗已经说明作品提交完成；此时原来的
            # 作品设置弹窗已经被卸载，不可能再读到 AI switch。第一次提交
            # 前已确认过关闭状态，不能在这里再次轮询 8 秒等待不存在的开关。
            if allow_missing and initial_ai_state == "off":
                _log("[xunfei]   作品设置弹窗已关闭，沿用第一次确认前已验证的 AI 标识关闭状态")
                return True
            state = ensure_ai_off()
            _log(f"[xunfei]   合成前 AI 标识开关状态: {state}")
            return state == "off"

        def confirm_state():
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            return state if state in {"confirm", "ai_modal", "order", "insufficient", "rate_limited", "login"} else None

        appeared = _poll(
            confirm_state,
            # 不假设“作品设置”会同步出现；讯飞客户端可能延迟挂载
            # 5–10 秒，继续轮询但每轮只读取一次状态快照。
            timeout=15,
            interval=0.6,
            max_interval=1.25,
            page=page,
            cancel_check=cancel_check,
        )
        if not appeared and self._visible_confirm_synth_buttons(page):
            appeared = "confirm"
        if not appeared:
            # 无弹窗也可能直接开始合成；若出现订单/错误则按其处理
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   未找到确认合成按钮，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            else:
                _log("[xunfei]   未找到确认合成按钮，当前没有可识别的可见弹窗")
            settled = wait_order(4) or "failed"
            if settled == "ok":
                # 某些版本没有弹出“确认合成”按钮，而是直接出现订单
                # 状态；订单本身已经证明提交发生，后续回读失败也不能重试。
                confirm_clicked = True
                self._confirm_click_succeeded = True
                if not ensure_ai_setting():
                    return uncertain_after_confirm(
                        "已出现订单但作品设置回读失败，提交结果不确定"
                    )
            elif settled == "failed":
                # 生成按钮已经点击，但页面没有给出可判定的确认/订单
                # 状态；不能把这个未知结果当成“未提交”再次点击。
                self._submission_state_uncertain = True
            return settled

        self._pause(page, 0.6, 0.3)

        # 讯飞“作品设置”弹窗中的格式是独立的 WAV/MP3 单选项。不能依赖
        # 默认勾选，也不能取第一个 option；提交前必须回读并确认 MP3。
        if not ensure_mp3():
            _log("[xunfei]   未能确认作品格式为 MP3，停止提交，避免误生成 WAV")
            return "failed"

        if works_name:
            self._set_works_name(page, works_name)

        # “作品设置”就是这次提交使用的最终设置，真实 DOM 中开关位于这里：
        # role="switch"、aria-checked="true"。必须在第一次确认合成前关闭，
        # 不能等弹窗切换或订单完成后再处理，否则水印配置已经被提交。
        initial_ai_state = ensure_ai_off()
        _log(f"[xunfei]   第一次确认前 AI 标识开关状态: {initial_ai_state}")
        if initial_ai_state != "off":
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(
                    "[xunfei]   AI 标识开关未找到可关闭的作品设置弹窗: "
                    + json.dumps(snapshot, ensure_ascii=False)[:1800]
                )
            _log("[xunfei]   AI 标识开关无法确认关闭，停止提交，避免生成带水印音频")
            return "failed"

        # 第一次点击"确认合成"
        clicked = self._click_confirm_synth_button(page)
        if not clicked:
            clicked = bool(_safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"))
        _log(f"[xunfei]   第一次确认合成: {'✓' if clicked else '✗'}")
        if not clicked:
            snapshot = _safe_eval(page, JS.SNAPSHOT_DIALOGS)
            if snapshot:
                _log(f"[xunfei]   第一次确认合成点击失败，当前可见弹窗: {json.dumps(snapshot, ensure_ascii=False)[:1800]}")
            return "failed"
        confirm_clicked = True
        self._confirm_click_succeeded = True

        outcome = observe_after_first_confirm()
        _log(f"[xunfei]   第一次确认后的页面状态: {outcome}")
        ai_modal_seen = outcome == "ai_modal"
        if outcome == "ai_modal":
            _log("[xunfei]   检测到 AI 标识说明弹窗")
            if not handle_ai_flag(False):
                _log("[xunfei]   AI 标识弹窗未完成确认，停止本次合成")
                return uncertain_after_confirm("确认合成后 AI 标识弹窗未完成，提交结果不确定")
        elif outcome in ("order", "insufficient", "rate_limited"):
            if outcome == "order" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm(
                    "确认合成后已出现订单，但作品设置回读失败"
                )
            return "ok" if outcome == "order" else outcome

        # AI 弹窗关闭、页面切换和确认合成按钮重新出现之间存在异步延迟。
        # 这里必须继续轮询状态，不能用一次立即查询把任务误判为已完成。
        def probe_followup():
            # 与第一次确认后的探测保持相同优先级；不要在一轮中重复执行
            # 多个 page.evaluate，延迟挂载时仍由外层轮询继续等待。
            info = _probe_synth_state(page)
            state = (info or {}).get("state")
            return state if state in {
                "ai_modal", "insufficient", "rate_limited", "login", "order", "confirm",
            } else None

        followup = _poll(
            probe_followup,
            timeout=15,
            interval=0.35,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        )
        if followup == "ai_modal":
            ai_modal_seen = True
            # 少数页面会在第一次 AI 弹窗确认后重新挂载一次弹窗，允许再处理一轮。
            _log("[xunfei]   AI 标识弹窗仍在，重新处理")
            if not handle_ai_flag(False):
                _log("[xunfei]   AI 标识弹窗二次处理失败，停止本次合成")
                return uncertain_after_confirm(
                    "确认合成后的 AI 标识弹窗未完成，提交结果不确定"
                )
            followup = _poll(
                probe_followup,
                timeout=12,
                interval=0.35,
                max_interval=1.0,
                page=page,
                cancel_check=cancel_check,
            )
        _log(f"[xunfei]   二次确认前页面状态: {followup or '未发现明确状态'}")
        if followup in ("order", "insufficient", "rate_limited"):
            if followup == "order" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("确认合成后已出现订单，但作品设置回读失败")
            return "ok" if followup == "order" else followup

        # 真实“作品设置”弹窗的结构是 role="switch" + aria-checked，
        # 它可能不会触发 AI 说明弹窗；二次确认前再次强制回读并关闭。
        if not ensure_ai_setting(allow_missing=True):
            return uncertain_after_confirm("确认合成后作品设置回读失败，提交结果不确定")
        clicked2 = bool(_poll(
            lambda: self._click_confirm_synth_button(page)
            or _safe_eval(page, JS.CLICK_BTN_IN_MODAL, "确认合成"),
            timeout=12,
            interval=0.35,
            max_interval=1.0,
            page=page,
            cancel_check=cancel_check,
        ))
        _log(f"[xunfei]   第二次确认合成: {'✓' if clicked2 else '✗'}")
        if clicked2:
            confirm_clicked = True
            self._confirm_click_succeeded = True
            settled = wait_order(90) or "ok"
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("二次确认后作品设置回读失败，提交结果不确定")
            return settled

        # 讯飞部分账号/版本在没有 AI 说明弹窗时，第一次“确认合成”就
        # 已经提交任务，不会再显示第二个确认按钮。等待一小段时间确认
        # 没有额度、登录或频控错误后，按已提交处理，避免误重试造成频控。
        settled = wait_order(12)
        if settled:
            if settled == "ok" and not ensure_ai_setting(allow_missing=True):
                return uncertain_after_confirm("确认合成后作品设置回读失败，提交结果不确定")
            return settled
        if ai_modal_seen:
            info = _probe_synth_state(page)
            if info and info.get("ai_modal"):
                return uncertain_after_confirm("确认合成后的 AI 标识弹窗仍未关闭，提交结果不确定")
        if not ensure_ai_setting(allow_missing=True):
            return uncertain_after_confirm("确认合成后无法确认作品设置，提交结果不确定")
        return "ok"
