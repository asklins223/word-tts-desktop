"""Audio and image upload controls for the visible page."""

from __future__ import annotations

from .page_shared import *  # noqa: F403,F401


class PlatformInputAssetMixin:
    """Resolve existing controls and upload only through file inputs."""

    def _audio_region_near_label(
        self,
        occurrence: int,
        *,
        label: str | None = None,
    ) -> Any | None:
        labels = (label,) if label else _AUDIO_LABELS
        for label_text in labels:
            nodes = self.page.get_by_text(label_text, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            matches: list[Any] = []
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    hit_level = _closest_visible_level(
                        node,
                        ".examAudioContent:visible",
                        max_level=8,
                    )
                    if hit_level > 0:
                        regions = _ancestor_at_level(node, hit_level).locator(
                            ".examAudioContent:visible"
                        )
                        if regions.count() == 1:
                            matches.append(regions.first)
                            continue
                    parent = node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        regions = parent.locator(".examAudioContent:visible")
                        if regions.count() == 1:
                            matches.append(regions.first)
                            break
                except Exception:
                    continue
            if occurrence < len(matches):
                return matches[occurrence]
        return None

    def _file_input_near_label(
        self,
        occurrence: int,
        *,
        label: str | None = None,
    ) -> Any | None:
        region = self._audio_region_near_label(occurrence, label=label)
        if region is not None:
            try:
                inputs = region.locator('input[type="file"]')
                if inputs.count() == 1:
                    return inputs.first
            except Exception:
                pass
            # 当前页面的上传控件属于字段自己的 examAudioContent。已有
            # 文件时该区域没有 input，不能再向上爬取共享 input，否则可能
            # 把原文音频误传到另一个仍为空的音频字段。
            return None

        labels = (label,) if label else _AUDIO_LABELS
        for label_text in labels:
            nodes = self.page.get_by_text(label_text, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            matches: list[Any] = []
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    hit_level = _closest_count_level(
                        node,
                        'input[type="file"]',
                        max_level=8,
                    )
                    if hit_level > 0:
                        inputs = _ancestor_at_level(node, hit_level).locator(
                            'input[type="file"]'
                        )
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            continue
                    parent = node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        inputs = parent.locator('input[type="file"]')
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            break
                except Exception:
                    continue
            if occurrence < len(matches):
                return matches[occurrence]
        return None

    def _audio_region_in_scope(self, scope: Any, label: str) -> Any | None:
        """在一张题卡内定位指定音频字段，避免同页字段序号串位。"""

        nodes = scope.get_by_text(label, exact=True)
        try:
            count = nodes.count()
        except Exception:
            count = 0
        for index in range(count):
            try:
                node = nodes.nth(index)
                if not node.is_visible():
                    continue
                hit_level = _closest_visible_level(
                    node,
                    ".examAudioContent:visible",
                    max_level=8,
                )
                if hit_level > 0:
                    regions = _ancestor_at_level(node, hit_level).locator(
                        ".examAudioContent:visible"
                    )
                    if regions.count() == 1:
                        return regions.first
                parent = node
                for _level in range(8):
                    parent = parent.locator("xpath=..")
                    regions = parent.locator(".examAudioContent:visible")
                    if regions.count() == 1:
                        return regions.first
            except Exception:
                continue
        return None

    def _clear_audio_in_scope(
        self,
        scope: Any,
        label: str,
        description: str,
    ) -> None:
        """清空题卡内已有的可选音频，完全通过页面删除控件完成。"""

        region = self._audio_region_in_scope(scope, label)
        if region is None:
            return
        try:
            deletes = region.locator(".deleteBtn img:visible")
            if deletes.count() == 0:
                return
            delete = deletes.first
            delete.click(timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(
                f"{description}清除已有{label}失败: {exc}"
            ) from exc

        def cleared() -> bool:
            current = self._audio_region_in_scope(scope, label)
            if current is None:
                return True
            try:
                return current.locator(".deleteBtn img:visible").count() == 0
            except Exception:
                return False

        self._wait_until(
            cleared,
            f"{description}清除{label}后页面仍显示旧音频",
            timeout_seconds=15,
            interval_ms=50,
        )

    def _audio_delete_near_label(
        self,
        occurrence: int,
        *,
        label: str | None = None,
    ) -> Any | None:
        region = self._audio_region_near_label(occurrence, label=label)
        if region is None:
            return None
        try:
            delete = region.locator(".deleteBtn img:visible")
            if delete.count():
                return delete.first
        except Exception:
            pass
        return None

    def _upload_audio(
        self,
        path: str,
        occurrence: int,
        *,
        label: str | None = None,
    ) -> None:
        input_locator = self._file_input_near_label(occurrence, label=label)
        if input_locator is None:
            # 已有音频时，页面把 file input 从 DOM 中移除，只保留播放图标和
            # 删除图标。先用页面自己的删除按钮清空该字段，控件才会重新
            # 出现；这仍然是页面编辑动作，不是接口写入。
            delete = self._audio_delete_near_label(occurrence, label=label)
            if delete is not None:
                try:
                    delete.click(timeout=self.action_timeout_ms)
                except Exception as exc:
                    raise PlatformInputUiError("清除已有音频后重新上传失败") from exc
                self._wait_until(
                    lambda: self._file_input_near_label(
                        occurrence,
                        label=label,
                    )
                    is not None,
                    f"清除第 {occurrence + 1} 个音频后上传控件没有出现",
                    timeout_seconds=15,
                    interval_ms=50,
                )
                input_locator = self._file_input_near_label(
                    occurrence,
                    label=label,
                )
        if input_locator is None and label is None:
            all_inputs = self.page.locator('input[type="file"][accept=".mp3,.wav"]')
            try:
                if occurrence < all_inputs.count():
                    input_locator = all_inputs.nth(occurrence)
            except Exception:
                input_locator = None
        if input_locator is None:
            raise PlatformInputUiError(
                f"页面没有找到第 {occurrence + 1} 个音频上传控件"
            )
        try:
            input_locator.set_input_files(path, timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"通过页面上传音频 {path} 失败: {exc}") from exc

    def _audio_file_input_in_scope(self, scope: Any, label: str) -> Any | None:
        """在同一题卡的指定音频字段内找上传控件。"""

        region = self._audio_region_in_scope(scope, label)
        if region is None:
            return None
        try:
            inputs = region.locator('input[type="file"]')
            if inputs.count() == 1:
                return inputs.first
        except Exception:
            pass
        return None

    def _wait_audio_rendered_in_scope(
        self,
        scope: Any,
        label: str,
        *,
        expected_path: str | None,
        description: str,
    ) -> None:
        """等待题卡内的音频字段显示本次上传的文件。"""

        def rendered() -> bool:
            region = self._audio_region_in_scope(scope, label)
            if region is None:
                return False
            displays = region.locator(".showNameContent:visible")
            audios = region.locator("audio[src]:visible")
            try:
                if expected_path and any(
                    self._audio_display_matches(text, expected_path)
                    for text in displays.all_inner_texts()
                ):
                    return True
                # If the UI exposes a filename, do not accept a stale or
                # differently scoped filename.  Some versions expose only an
                # audio preview, in which case the field was empty before
                # set_input_files and the preview is the available evidence.
                if expected_path and displays.count():
                    return False
                return bool(audios.count())
            except Exception:
                return False

        self._wait_until(
            rendered,
            f"{description}{label}上传后页面没有显示文件",
            timeout_seconds=60,
            interval_ms=50,
        )

    def _upload_audio_in_scope(
        self,
        scope: Any,
        path: str,
        *,
        label: str,
        description: str,
    ) -> None:
        """替换题卡内的音频，避免按整页 occurrence 串到别的字段。"""

        input_locator = self._audio_file_input_in_scope(scope, label)
        if input_locator is None:
            region = self._audio_region_in_scope(scope, label)
            if region is None:
                raise PlatformInputUiError(
                    f"{description}没有找到题卡内的“{label}”音频字段"
                )
            try:
                delete = region.locator(".deleteBtn img:visible")
                if delete.count():
                    delete.first.click(timeout=self.action_timeout_ms)
            except Exception as exc:
                raise PlatformInputUiError(
                    f"{description}清除题卡内已有{label}失败: {exc}"
                ) from exc
            self._wait_until(
                lambda: self._audio_file_input_in_scope(scope, label) is not None,
                f"{description}清除已有{label}后上传控件没有出现",
                timeout_seconds=15,
                interval_ms=50,
            )
            input_locator = self._audio_file_input_in_scope(scope, label)
        if input_locator is None:
            raise PlatformInputUiError(
                f"{description}没有找到题卡内的“{label}”上传控件"
            )
        try:
            input_locator.set_input_files(path, timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(
                f"{description}通过页面上传{label} {path} 失败: {exc}"
            ) from exc
        self._wait_audio_rendered_in_scope(
            scope,
            label,
            expected_path=path,
            description=description,
        )

    @staticmethod
    def _audio_display_matches(display_text: str, expected_path: str) -> bool:
        """判断页面显示的文件名是否对应本次上传的文件。"""

        expected_name = re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+",
            "",
            Path(expected_path).stem,
        ).lower()
        actual_name = re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+",
            "",
            str(display_text),
        ).lower()
        return bool(expected_name) and expected_name in actual_name

    def _wait_audio_rendered(
        self,
        occurrence: int,
        *,
        label: str | None = None,
        expected_path: str | None = None,
    ) -> None:
        labels = (label,) if label else _AUDIO_LABELS

        def rendered() -> bool:
            for label_text in labels:
                region = self._audio_region_near_label(
                    occurrence,
                    label=label_text,
                )
                if region is None:
                    continue
                displays = region.locator(".showNameContent:visible")
                audios = region.locator("audio[src]:visible")
                if expected_path:
                    try:
                        if any(
                            self._audio_display_matches(text, expected_path)
                            for text in displays.all_inner_texts()
                        ):
                            return True
                    except Exception:
                        continue
                elif displays.count() or audios.count():
                    return True
            return False

        self._wait_until(
            rendered,
            f"第 {occurrence + 1} 个音频上传后页面没有显示文件",
            timeout_seconds=60,
            interval_ms=50,
        )

    def _file_input_near_text(
        self,
        labels: Sequence[str],
        occurrence: int = 0,
    ) -> Any | None:
        """按上传提示文字找非音频文件控件，例如信息记录表图片。"""

        for label in labels:
            nodes = self.page.get_by_text(label, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            matches: list[Any] = []
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    hit_level = _closest_count_level(
                        node,
                        'input[type="file"]',
                        max_level=8,
                    )
                    if hit_level > 0:
                        inputs = _ancestor_at_level(node, hit_level).locator(
                            'input[type="file"]'
                        )
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            continue
                    parent = node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        inputs = parent.locator('input[type="file"]')
                        if inputs.count() == 1:
                            matches.append(inputs.first)
                            break
                except Exception:
                    continue
            if occurrence < len(matches):
                return matches[occurrence]
        return None

    def _image_region(self, occurrence: int = 0) -> Any | None:
        """定位信息记录表图片区域，兼容空上传和已有图片两种状态。"""

        labels = (
            "点击上传 支持 JPG/PNG",
            "支持 JPG/PNG",
            "重新上传",
        )
        for label in labels:
            matches: list[Any] = []
            nodes = self.page.get_by_text(label, exact=True)
            try:
                count = nodes.count()
            except Exception:
                count = 0
            for index in range(count):
                try:
                    node = nodes.nth(index)
                    if not node.is_visible():
                        continue
                    parent = node
                    for _level in range(8):
                        parent = parent.locator("xpath=..")
                        file_inputs = parent.locator('input[type="file"]')
                        delete = parent.get_by_text("删除", exact=True)
                        if file_inputs.count() == 1 or delete.count() == 1:
                            matches.append(parent)
                            break
                except Exception:
                    continue
            if occurrence < len(matches):
                return matches[occurrence]
        return None

    def _saved_image(self, occurrence: int = 0) -> Any | None:
        """定位页面已保存的图片本体。

        记录页在图片已经存在时只渲染 ``.contentImgBorder img``，不会把
        “重新上传”和对应的文件 input 放进可用 DOM；鼠标移到图片上后，
        页面才会挂载替换菜单。因此不能只按上传文案查找图片控件。
        """

        nodes = self.page.locator(".contentImgBorder img")
        matches: list[Any] = []
        try:
            count = nodes.count()
        except Exception:
            count = 0
        for index in range(count):
            try:
                node = nodes.nth(index)
                if node.is_visible():
                    matches.append(node)
            except Exception:
                continue
        if occurrence < len(matches):
            return matches[occurrence]
        return None

    def _activate_saved_image_editor(self, occurrence: int = 0) -> None:
        """通过页面已有图片的悬浮菜单挂载“重新上传”控件。"""

        image = self._saved_image(occurrence)
        if image is None:
            return
        try:
            # 页面菜单由图片悬浮状态控制。这里只移动鼠标来挂载菜单，不能
            # 点击图片：点击会触发浏览器原生的文件选择器。真正的上传统一
            # 通过后面的 input[type=file].set_input_files 完成。
            image.hover(force=True, timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(
                f"激活第 {occurrence + 1} 个信息记录表图片上传菜单失败: {exc}"
            ) from exc
        self._wait_until(
            lambda: self._image_region(occurrence) is not None,
            f"第 {occurrence + 1} 个已有信息记录表图片没有出现替换控件",
            timeout_seconds=10,
            interval_ms=50,
        )

    def _image_delete(self, occurrence: int = 0) -> Any | None:
        region = self._image_region(occurrence)
        if region is None:
            return None
        try:
            delete = self._first_visible(region.get_by_text("删除", exact=True))
            if delete is not None:
                return delete
        except Exception:
            pass
        return None

    def _upload_image(self, path: str, occurrence: int = 0) -> None:
        region = self._image_region(occurrence)
        if region is None:
            # 编辑既有试卷时，图片本体先于悬浮菜单挂载；先通过页面图片
            # 控件激活“重新上传”，再重新定位其 JPG/PNG 文件 input。
            self._activate_saved_image_editor(occurrence)
            region = self._image_region(occurrence)
        if region is None:
            # 页面对记录部分使用视口级懒加载；填写听力原文后视口可能已
            # 滚到下方，顶部的图片上传控件会暂时不在 DOM。先把记录标题
            # 滚回视口，再重新查找页面自己的上传区域。
            heading = self._first_visible(
                self.page.get_by_text(
                    re.compile(r"^听后记录.*共.*题")
                )
            )
            if heading is not None:
                try:
                    heading.scroll_into_view_if_needed()
                    self.page.wait_for_timeout(300)
                except Exception:
                    pass
                region = self._image_region(occurrence)
        input_locator = None
        if region is not None:
            try:
                inputs = region.locator('input[type="file"]')
                if inputs.count() == 1:
                    input_locator = inputs.first
            except Exception:
                pass
        if input_locator is None:
            delete = self._image_delete(occurrence)
            if delete is not None:
                try:
                    delete.click(timeout=self.action_timeout_ms)
                except Exception as exc:
                    raise PlatformInputUiError("清除已有信息记录表图片失败") from exc
                self._wait_until(
                    lambda: self._image_region(occurrence) is not None
                    and self._image_region(occurrence).locator(
                        'input[type="file"]'
                    ).count()
                    == 1,
                    f"清除第 {occurrence + 1} 个信息记录表图片后上传控件没有出现",
                    timeout_seconds=15,
                    interval_ms=50,
                )
                region = self._image_region(occurrence)
                if region is not None:
                    input_locator = region.locator('input[type="file"]').first
        if input_locator is None:
            input_locator = self._file_input_near_text(
                ("点击上传 支持 JPG/PNG", "支持 JPG/PNG"),
                occurrence,
            )
        if input_locator is None:
            raise PlatformInputUiError(
                f"页面没有找到第 {occurrence + 1} 个信息记录表图片上传控件"
            )
        try:
            input_locator.set_input_files(path, timeout=self.action_timeout_ms)
        except Exception as exc:
            raise PlatformInputUiError(f"通过页面上传图片 {path} 失败: {exc}") from exc
        self._wait_image_rendered(occurrence)

    def _wait_image_rendered(self, occurrence: int = 0) -> None:
        """确认图片上传控件已经渲染新图片，而不是只接受了文件选择。"""

        def rendered() -> bool:
            if self._saved_image(occurrence) is not None:
                return True
            region = self._image_region(occurrence)
            if region is None:
                return False
            try:
                return region.locator("img:visible").count() > 0
            except Exception:
                return False

        self._wait_until(
            rendered,
            f"第 {occurrence + 1} 个信息记录表图片上传后页面没有显示图片",
            timeout_seconds=60,
            interval_ms=50,
        )
