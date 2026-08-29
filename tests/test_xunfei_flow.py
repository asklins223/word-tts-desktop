from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import xunfei
import xunfei.downloads as xunfei_downloads
import xunfei.runtime as xunfei_runtime
from xunfei import XunFeiSession


class _FakeKeyboard:
    def __init__(self):
        self.inserted = []
        self.typed = []

    def insert_text(self, value):
        self.inserted.append(value)

    def type(self, value):
        self.typed.append(value)


class _FakePage:
    def __init__(self):
        self.keyboard = _FakeKeyboard()


class _HealthyFakeSession:
    _logged_in = True
    _page = object()
    _ctx = object()


class _PostConfirmPage:
    """只模拟确认后的状态探测，用于验证 AI 弹窗优先级。"""

    def __init__(self, *, ai_modal=False, rate_limited=False):
        self.ai_modal = ai_modal
        self.rate_limited = rate_limited

    def evaluate(self, script, arg=None):
        if script == xunfei.JS.PROBE_SYNTH_STATE:
            if self.ai_modal:
                return {
                    "state": "ai_modal",
                    "ai_modal": True,
                    "ai_switch": "not_found",
                }
            if self.rate_limited:
                return {
                    "state": "rate_limited",
                    "ai_modal": False,
                    "ai_switch": "not_found",
                }
            return {"state": None, "ai_modal": False, "ai_switch": "not_found"}
        if script == xunfei.JS.CHECK_MODAL_HAS_TEXT:
            return self.ai_modal
        if script == xunfei.JS.CHECK_RATE_LIMITED:
            return self.rate_limited
        if script in {
            xunfei.JS.CHECK_INSUFFICIENT,
            xunfei.JS.CHECK_GO_DOWNLOAD,
            xunfei.JS.CHECK_FREE_MODAL,
        }:
            return False
        if script == xunfei.JS.SNAPSHOT_DIALOGS:
            return []
        return None

    def wait_for_timeout(self, _milliseconds):
        return None


class XunfeiFlowTests(unittest.TestCase):
    def test_common_voice_without_speaker_number_is_resolved_by_page(self):
        self.assertIsNone(
            xunfei.XunFeiSession._speaker_number(
                "common:10001135",
                {"name": "欣畅", "common_id": 10001135},
            )
        )

    def test_default_voice_fallback_keeps_multi_speaker_identifiers(self):
        self.assertGreater(xunfei.VOICES["amanda"]["speaker_no"], 0)
        self.assertGreater(xunfei.VOICES["george"]["speaker_no"], 0)

        with mock.patch.dict(
            xunfei.VOICES,
            {"amanda": {"name": "Amanda", "gender": "female", "speaker_no": 544508087}},
            clear=False,
        ):
            xunfei.register_voice_catalog([{
                "key": "amanda",
                "name": "Amanda",
                "gender": "female",
                "speaker_no": None,
            }])
            self.assertEqual(xunfei.VOICES["amanda"]["speaker_no"], 544508087)

    def test_composite_ui_rows_preserve_item_boundaries_and_expand_editor_lines(self):
        session = XunFeiSession()
        work = {
            "boundary_ms": 2000,
            "items": [
                {
                    "item_id": "q1",
                    "segments": [
                        {"voice_key": "amanda", "text": ""},
                        {"voice_key": "amanda", "text": "First line\nSecond line"},
                    ],
                },
                {
                    "item_id": "q2",
                    "segments": [{"voice_key": "george", "text": "Third line"}],
                },
            ],
        }

        rows, boundaries = session._composite_ui_rows(work)

        self.assertEqual([row["text"] for row in rows], [
            "First line", "Second line", "Third line",
        ])
        self.assertEqual([row["item_index"] for row in rows], [0, 0, 1])
        self.assertEqual(boundaries, [(1, 2000)])

    def test_composite_pause_insertion_opens_menu_and_inserts_target_marker(self):
        """停顿入口先开菜单时，必须点击目标时长并回读页面节点。"""
        from playwright.sync_api import sync_playwright

        html = """
        <style>
          #pause-menu { position: fixed; left: 20px; top: 40px; }
          .ssml-editor { width: 640px; border: 1px solid #999; }
        </style>
        <div id="toolbar">
          <button id="pause-trigger" type="button" aria-label="停顿">停顿</button>
          <div id="pause-menu" style="display:none">
            <div class="cursor-pointer" data-value="500">0.5s</div>
            <div class="cursor-pointer" data-value="2000">2 秒</div>
          </div>
        </div>
        <div class="ssml-editor" contenteditable="true">
          <p><span class="ssml-text-mark-speaker">
            <b class="ssml-tag" data-type="range_anchor">Amanda</b>
            <span class="range-annotation-content speaker-content">First line.</span>
          </span></p>
          <p><span class="ssml-text-mark-speaker">
            <b class="ssml-tag" data-type="range_anchor">George</b>
            <span class="range-annotation-content speaker-content">Second line.</span>
          </span></p>
        </div>
        <script>
          // 真实编辑器的工具栏会在 mousedown 时保留 Selection，避免
          // 点击菜单后选区被工具栏自身夺走；测试桩也模拟这个页面契约。
          const savedSelections = new Map();
          const saveSelection = () => {
            const selection = window.getSelection();
            if (!selection || !selection.rangeCount) return;
            savedSelections.set('pause', selection.getRangeAt(0).cloneRange());
          };
          document.getElementById('pause-trigger').addEventListener('mousedown', (event) => {
            saveSelection();
            event.preventDefault();
          });
          document.getElementById('pause-trigger').onclick = () => {
            document.getElementById('pause-menu').style.display = 'block';
          };
          document.querySelectorAll('#pause-menu [data-value]').forEach((option) => {
            option.addEventListener('mousedown', (event) => {
              const range = savedSelections.get('pause');
              if (range) {
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
              }
              event.preventDefault();
            });
            option.onclick = () => {
              const selection = window.getSelection();
              if (!selection || !selection.rangeCount) return;
              const range = selection.getRangeAt(0);
              const anchor = selection.anchorNode;
              const paragraph = anchor && (anchor.parentElement || anchor).closest('p');
              if (!paragraph || !range.collapsed) return;
              const marker = document.createElement('span');
              marker.className = 'ssml-tag pause-marker';
              marker.setAttribute('data-type', 'break');
              marker.setAttribute('data-value', option.dataset.value);
              range.insertNode(marker);
            };
          });
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                XunFeiSession._insert_composite_pause(page, 0, 2000)
                self.assertEqual(
                    page.locator('.ssml-editor p').nth(0)
                    .locator('[data-type="break"][data-value="2000"]').count(),
                    1,
                )
                self.assertEqual(
                    XunFeiSession._read_composite_pause_issues(
                        page, [(0, 2000)]
                    ),
                    [],
                )
                self.assertEqual(
                    page.locator('.ssml-editor p').nth(0)
                    .locator('.speaker-content').inner_text(),
                    'First line.',
                )
                self.assertEqual(
                    page.locator('.ssml-editor p').nth(0)
                    .locator('[data-type="break"][data-value="2000"]')
                    .evaluate("el => el.previousSibling && el.previousSibling.textContent"),
                    'First line.',
                )
            finally:
                browser.close()

    def test_composite_pause_fast_path_succeeds_without_heavy_fallback(self):
        """工具栏直接带 2s 按钮的页面：折叠光标主路径必须完成插入。

        该测试同时是“主路径优先”的逻辑证据——重型兜底
        （原生 select_text + 整页元数据扫描 + 停顿菜单路径）在此场景下
        一次都不应被调用，单处停顿的协议往返保持原脚本水平。
        """
        from playwright.sync_api import sync_playwright
        from unittest import mock

        html = """
        <style>.ssml-editor { width: 640px; border: 1px solid #999; }</style>
        <div id="toolbar">
          <button id="pause-2s" type="button">2s</button>
        </div>
        <div class="ssml-editor" contenteditable="true">
          <p><span class="ssml-text-mark-speaker">
            <b class="ssml-tag" data-type="range_anchor">Amanda</b>
            <span class="range-annotation-content speaker-content">First line.</span>
          </span></p>
          <p><span class="ssml-text-mark-speaker">
            <b class="ssml-tag" data-type="range_anchor">George</b>
            <span class="range-annotation-content speaker-content">Second line.</span>
          </span></p>
        </div>
        <script>
          const savedSelection = { range: null };
          document.getElementById('pause-2s').addEventListener('mousedown', (event) => {
            const selection = window.getSelection();
            if (selection && selection.rangeCount) {
              savedSelection.range = selection.getRangeAt(0).cloneRange();
            }
            event.preventDefault();
          });
          document.getElementById('pause-2s').onclick = () => {
            const selection = window.getSelection();
            if (!selection || !selection.rangeCount) return;
            const range = selection.getRangeAt(0);
            const anchor = selection.anchorNode;
            const paragraph = anchor && (anchor.parentElement || anchor).closest('p');
            if (!paragraph || !range.collapsed) return;
            const marker = document.createElement('span');
            marker.className = 'ssml-tag pause-marker';
            marker.setAttribute('data-type', 'break');
            marker.setAttribute('data-value', '2000');
            range.insertNode(marker);
          };
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                with mock.patch.object(
                    XunFeiSession,
                    "_click_composite_pause_control",
                    side_effect=AssertionError("heavy fallback must not run when the fast path works"),
                ), mock.patch.object(
                    XunFeiSession,
                    "_click_composite_ui_control",
                    wraps=XunFeiSession._click_composite_ui_control,
                ) as direct_click:
                    XunFeiSession._insert_composite_pause(page, 0, 2000)
                direct_click.assert_called()
                self.assertEqual(
                    page.locator('.ssml-editor p').nth(0)
                    .locator('[data-type="break"][data-value="2000"]').count(),
                    1,
                )
                self.assertEqual(
                    page.locator('.ssml-editor p').nth(0)
                    .locator('[data-type="break"][data-value="2000"]')
                    .evaluate("el => el.previousSibling && el.previousSibling.textContent"),
                    'First line.',
                )
                self.assertEqual(
                    XunFeiSession._read_composite_pause_issues(page, [(0, 2000)]),
                    [],
                )
            finally:
                browser.close()

    def test_composite_pause_duration_matching_rejects_wrong_duration(self):
        self.assertTrue(
            XunFeiSession._composite_pause_metadata_matches(
                {"text": "插入停顿 2 秒"}, 2000
            )
        )
        self.assertTrue(
            XunFeiSession._composite_pause_metadata_matches(
                {"dataType": "break", "dataValue": "2"}, 2000
            )
        )
        self.assertFalse(
            XunFeiSession._composite_pause_metadata_matches(
                {"text": "1s"}, 2000
            )
        )

    def test_composite_rows_batch_only_contiguous_equal_signatures(self):
        session = XunFeiSession()
        rows = [
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
        ]

        self.assertEqual(session._composite_row_groups(rows), [
            (0, 1), (2, 2), (3, 3),
        ])

    def test_composite_marking_plan_reduces_alternating_voice_operations(self):
        session = XunFeiSession()
        rows = [
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
        ]

        plan = session._composite_marking_plan(rows)

        # 原方案需要 4 次连续选区设置；新方案先全文设置 Amanda，
        # 再只修正两个 George 区间，最终每一行仍有明确的目标配置。
        self.assertEqual(plan["base_index"], 0)
        self.assertEqual(plan["correction_groups"], [(1, 1), (3, 3)])
        self.assertEqual(plan["contiguous_group_count"], 4)

    def test_composite_marking_plan_uses_most_frequent_full_signature(self):
        session = XunFeiSession()
        rows = [
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 65, "pitch": 50, "volume": 50},
        ]

        plan = session._composite_marking_plan(rows)

        self.assertEqual(plan["base_index"], 0)
        self.assertEqual(plan["correction_groups"], [(1, 1), (3, 3)])

    def test_composite_signature_ranges_batch_sparse_roles_by_configuration(self):
        session = XunFeiSession()
        rows = [
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
        ]

        plan = session._composite_signature_ranges(rows)

        self.assertEqual(
            [entry["ranges"] for entry in plan],
            [[(0, 0), (2, 2)], [(1, 1), (3, 3)]],
        )

    def test_composite_signature_ranges_keeps_parameters_separate(self):
        session = XunFeiSession()
        rows = [
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 60, "pitch": 50, "volume": 50},
            {"voice_key": "amanda", "speed": 50, "pitch": 50, "volume": 50},
        ]

        plan = session._composite_signature_ranges(rows)

        self.assertEqual(
            [entry["ranges"] for entry in plan],
            [[(0, 0), (2, 2)], [(1, 1)]],
        )

    def test_long_editor_selection_keeps_one_batch_across_scroll(self):
        """长编辑器不可同时看见首尾时，选区仍不能退化成逐行处理。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - 构建环境会安装依赖
            self.skipTest(f"Playwright 未安装: {error}")

        texts = [f"Line {index}: preserve the complete beginning and ending." for index in range(10)]
        rows = [{"text": text} for text in texts]
        html = """
            <style>
                .ssml-editor {
                    width: 520px;
                    height: 110px;
                    overflow: auto;
                    border: 1px solid #999;
                }
                .ssml-editor p { margin: 0; padding: 8px; }
            </style>
            <div class="ssml-editor" contenteditable="true">
                %s
            </div>
        """ % "".join(f"<p>{text}</p>" for text in texts)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 700, "height": 260})
                page.set_content(html)
                selected = XunFeiSession._select_editor_rows(page, rows, 1, 8)
                self.assertEqual(
                    XunFeiSession._normalize_selection_text(selected),
                    XunFeiSession._normalize_selection_text("".join(texts[1:9])),
                )
            finally:
                browser.close()

    def test_selection_readback_ignores_existing_speaker_labels(self):
        """修正已标注区间时，选区校验只比较正文，不把音色标签算进去。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - 构建环境会安装依赖
            self.skipTest(f"Playwright 未安装: {error}")

        html = """
            <div class="ssml-editor" contenteditable="true">
                <p><span class="ssml-text-mark-speaker" data-label="Amanda-教育">
                    <b contenteditable="false" class="ssml-tag">
                        <span class="ssml-tag-label">Amanda-教育</span>
                    </b>
                    <span class="range-annotation-content speaker-content">I’m fine, thanks.</span>
                </span></p>
                <p><span class="ssml-text-mark-speaker" data-label="Amanda-教育">
                    <b contenteditable="false" class="ssml-tag">
                        <span class="ssml-tag-label">Amanda-教育</span>
                    </b>
                    <span class="range-annotation-content speaker-content">Hello! I’m Jack.</span>
                </span></p>
            </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                selected = page.evaluate(
                    xunfei.JS.SELECT_EDITOR_RANGE,
                    [0, 1],
                )
                self.assertIsNotNone(selected)
                self.assertEqual(
                    XunFeiSession._normalize_selection_text(selected),
                    XunFeiSession._normalize_selection_text(
                        "I’m fine, thanks.Hello! I’m Jack."
                    ),
                )
                actual = page.evaluate(xunfei.JS.GET_SELECTION_TEXT)
                self.assertEqual(
                    XunFeiSession._normalize_selection_text(actual),
                    XunFeiSession._normalize_selection_text(
                        "I’m fine, thanks.Hello! I’m Jack."
                    ),
                )
                selected_by_ui = XunFeiSession._select_editor_rows(
                    page,
                    [
                        {"text": "I’m fine, thanks."},
                        {"text": "Hello! I’m Jack."},
                    ],
                    0,
                    1,
                )
                self.assertEqual(
                    XunFeiSession._normalize_selection_text(selected_by_ui),
                    XunFeiSession._normalize_selection_text(
                        "I’m fine, thanks.Hello! I’m Jack."
                    ),
                )
            finally:
                browser.close()

    def test_composite_voice_card_prefers_search_result_over_recent_chip(self):
        """同名音色同时出现在搜索结果和最近使用区时不能误点快捷卡片。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - 构建环境会安装依赖
            self.skipTest(f"Playwright 未安装: {error}")

        html = """
            <div class="fixed" style="display:block; width:800px; height:500px">
                <input placeholder="输入主播名称进行搜索" />
                <div class="w-full rounded-lg cursor-pointer">
                    <img alt="英语-Amanda" />
                    <span>Amanda</span>
                </div>
                <button class="cursor-pointer">
                    <img alt="Amanda" />
                    <span>Amanda</span>
                    <span>语速 50 语调 50 音量 50</span>
                </button>
                <button class="apply">使用</button>
            </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                card = XunFeiSession._find_composite_voice_card(page, "Amanda")
                self.assertIsNotNone(card)
                self.assertEqual(card.evaluate("el => el.tagName"), "DIV")
                self.assertEqual(card.locator("img").first.get_attribute("alt"), "英语-Amanda")
            finally:
                browser.close()

    def test_composite_panel_does_not_reuse_right_sidebar_search(self):
        """右侧栏也有同名搜索框时，必须先打开并使用多人配音弹层。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - 构建环境会安装依赖
            self.skipTest(f"Playwright 未安装: {error}")

        html = """
            <aside id="right-sidebar">
                <input id="sidebar-search" placeholder="搜索主播 / 标签" />
                <button id="sidebar-card">
                    <img alt="右侧栏-Amanda" />
                    <span>Amanda</span>
                </button>
            </aside>
            <button id="open-composite">多人配音</button>
            <script>
                document.getElementById('open-composite').addEventListener('click', () => {
                    const panel = document.createElement('div');
                    panel.id = 'composite-panel';
                    panel.className = 'fixed';
                    panel.style = 'display:block; width:800px; height:500px';
                    panel.innerHTML = `
                        <input id="composite-search" placeholder="搜索主播 / 标签" />
                        <div id="composite-card" class="w-full cursor-pointer">
                            <img alt="多人配音-Amanda" />
                            <span>Amanda</span>
                        </div>
                        <button>使用</button>
                    `;
                    document.body.appendChild(panel);
                });
            </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                search = XunFeiSession._open_composite_voice_panel(page)
                self.assertEqual(search.get_attribute("id"), "composite-search")
                self.assertEqual(
                    XunFeiSession._composite_ui_scope(page).get_attribute("id"),
                    "composite-panel",
                )
                card = XunFeiSession._find_composite_voice_card(page, "Amanda")
                self.assertEqual(card.get_attribute("id"), "composite-card")
                self.assertEqual(
                    page.locator("#sidebar-search").input_value(),
                    "",
                )
            finally:
                browser.close()

    def test_composite_voice_mark_validation_rejects_mixed_or_wrong_rows(self):
        """存在错音色、重复标记或参数漂移时必须拒绝继续提交。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:  # pragma: no cover - 构建环境会安装依赖
            self.skipTest(f"Playwright 未安装: {error}")

        html = """
            <div class="ssml-editor" contenteditable="true">
                <p><span class="ssml-text-mark-speaker" data-speaker-id="544508087"
                    data-rate="50" data-pitch="50" data-volume="50">
                    <b class="ssml-tag" data-type="range_anchor">Amanda-教育</b>
                    <span class="range-annotation-content speaker-content">Hello.</span>
                </span></p>
            </div>
        """
        rows = [{"text": "Hello.", "speed": 50, "pitch": 50, "volume": 50}]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(
                    XunFeiSession._verify_composite_voice_marks(
                        page, rows, 0, 0, "Amanda", 544508087, rows[0]
                    )
                )
                page.locator(".ssml-text-mark-speaker").evaluate(
                    "el => el.setAttribute('data-speaker-id', '593031758')"
                )
                self.assertFalse(
                    XunFeiSession._verify_composite_voice_marks(
                        page, rows, 0, 0, "Amanda", 544508087, rows[0]
                    )
                )
            finally:
                browser.close()

    def test_composite_generation_uses_visible_page_flow_without_direct_submit_api(self):
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page
        work = {
            "work_id": "composite:ui",
            "works_name": "ui-test",
            "item_ids": ["q1"],
            "item_count": 1,
            "items": [{
                "segments": [{
                    "voice_key": "amanda",
                    "text": "hello",
                    "speed": 50,
                    "pitch": 50,
                    "volume": 50,
                }],
            }],
        }

        with mock.patch.object(session, "_prepare_composite_editor") as prepare, \
                mock.patch.object(session, "_mark_works_cutoff") as mark_cutoff, \
                mock.patch.object(session, "_click_generate") as click_generate, \
                mock.patch.object(session, "_confirm_synth", return_value="ok"), \
                mock.patch.object(session, "_consume_works_id", return_value="final-id"), \
                mock.patch.object(session, "_cleanup_after_item") as cleanup, \
                mock.patch.object(
                    session,
                    "_signed_api_post",
                    side_effect=AssertionError("多人配音不能直接调用 video-api 提交"),
                ):
            pending = session._generate_pending_composite(work)

        prepare.assert_called_once_with(page, work)
        mark_cutoff.assert_called_once_with()
        click_generate.assert_called_once_with(page)
        cleanup.assert_called_once_with(page)
        self.assertEqual(pending["works_id"], "final-id")

    def test_post_submit_cleanup_failure_does_not_retry_single_generation(self):
        """拿到 worksId 后页面清理失败不能再次提交并重复计费。"""
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page

        with mock.patch.object(session, "_select_voice"), \
                mock.patch.object(session, "_apply_params"), \
                mock.patch.object(session, "_input_text", return_value=True), \
                mock.patch.object(session, "_mark_works_cutoff"), \
                mock.patch.object(session, "_click_generate"), \
                mock.patch.object(session, "_confirm_synth", return_value="ok"), \
                mock.patch.object(session, "_consume_works_id", return_value="paid-once"), \
                mock.patch.object(
                    session,
                    "_cleanup_after_item",
                    side_effect=RuntimeError("editor cleanup failed"),
                ), \
                mock.patch.object(session, "_recover_and_retry") as recover:
            pending = session._generate_pending_one(
                "hello",
                voice_key="amanda",
                max_retries=2,
            )

        recover.assert_not_called()
        self.assertEqual(pending["works_id"], "paid-once")

    def test_post_submit_cleanup_failure_does_not_retry_composite_generation(self):
        """多人配音拿到 worksId 后清理失败也不能重复提交。"""
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page
        work = {
            "work_id": "composite:cleanup",
            "works_name": "cleanup-test",
            "item_ids": ["q1"],
            "item_count": 1,
            "items": [],
        }

        with mock.patch.object(session, "_prepare_composite_editor"), \
                mock.patch.object(session, "_mark_works_cutoff"), \
                mock.patch.object(session, "_click_generate"), \
                mock.patch.object(session, "_confirm_synth", return_value="ok"), \
                mock.patch.object(session, "_consume_works_id", return_value="paid-once"), \
                mock.patch.object(
                    session,
                    "_cleanup_after_item",
                    side_effect=RuntimeError("editor cleanup failed"),
                ), \
                mock.patch.object(session, "_recover_and_retry") as recover:
            pending = session._generate_pending_composite(work, max_retries=2)

        recover.assert_not_called()
        self.assertEqual(pending["works_id"], "paid-once")

    def test_confirmed_single_submit_without_works_id_is_not_retried(self):
        """确认按钮已成功点击但漏捕获 ID 时，最多只能进入对账。"""
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page

        with mock.patch.object(session, "_select_voice"), \
                mock.patch.object(session, "_apply_params"), \
                mock.patch.object(session, "_input_text", return_value=True), \
                mock.patch.object(session, "_mark_works_cutoff"), \
                mock.patch.object(session, "_click_generate") as click_generate, \
                mock.patch.object(session, "_confirm_synth", return_value="ok"), \
                mock.patch.object(session, "_consume_works_id", return_value=None), \
                mock.patch.object(session, "_recover_works_id_by_name", return_value=None), \
                mock.patch.object(session, "_recover_and_retry") as recover:
            with self.assertRaises(xunfei.XunfeiSubmissionAmbiguous) as raised:
                session._generate_pending_one(
                    "hello",
                    voice_key="amanda",
                    max_retries=3,
                )

        click_generate.assert_called_once_with(page)
        recover.assert_not_called()
        self.assertTrue(raised.exception.submission_confirmed)
        self.assertTrue(raised.exception.works_name)

    def test_confirmed_composite_submit_without_works_id_is_not_retried(self):
        """多人配音确认成功但漏捕获 ID 也不能走通用重试。"""
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page
        work = {
            "work_id": "composite:ambiguous",
            "works_name": "ambiguous-composite",
            "item_ids": ["q1"],
            "item_count": 1,
            "items": [],
        }

        with mock.patch.object(session, "_prepare_composite_editor"), \
                mock.patch.object(session, "_mark_works_cutoff"), \
                mock.patch.object(session, "_click_generate") as click_generate, \
                mock.patch.object(session, "_confirm_synth", return_value="ok"), \
                mock.patch.object(session, "_consume_works_id", return_value=None), \
                mock.patch.object(session, "_recover_works_id_by_name", return_value=None), \
                mock.patch.object(session, "_recover_and_retry") as recover:
            with self.assertRaises(xunfei.XunfeiSubmissionAmbiguous):
                session._generate_pending_composite(work, max_retries=3)

        click_generate.assert_called_once_with(page)
        recover.assert_not_called()

    def test_temporary_works_id_alone_never_becomes_download_id(self):
        """多人接口的临时 ID 不能在正式 ID 缺失时被当作可下载 ID。"""
        session = XunFeiSession()
        now = time.time()
        with session._works_lock:
            session._works_cutoff = now - 1
            session._works_entries = [("temporary-id", now)]
            session._temporary_works_entries = [("temporary-id", now)]

        self.assertIsNone(session._consume_works_id(timeout=0.02))

    def test_batch_reports_ambiguous_submission_without_attempting_download(self):
        session = XunFeiSession()
        session._logged_in = True
        events = []
        generated = []
        download_calls = []

        def ambiguous_generate(_text, **_kwargs):
            generated.append(_text)
            raise xunfei.XunfeiSubmissionAmbiguous(
                "已确认提交但未捕获 worksId",
                works_name="wordtts_paid_once",
            )

        def should_not_download(pending, **_kwargs):
            download_calls.append(list(pending))
            return {}

        session._generate_pending_one = ambiguous_generate
        session._download_pending_batch = should_not_download
        result = session.synth_batch([{
            "job_id": "ambiguous-job",
            "text": "hello",
            "voice_key": "amanda",
        }], progress_callback=events.append)

        self.assertEqual(generated, ["hello"])
        self.assertEqual(download_calls, [[]])
        self.assertTrue(result["ambiguous-job"]["ambiguous_works_id"])
        self.assertEqual(result["ambiguous-job"]["works_name"], "wordtts_paid_once")
        self.assertTrue(any(event.get("ambiguous_works_id") for event in events))

    def test_batch_reconciles_ambiguous_name_without_submitting_again(self):
        session = XunFeiSession()
        session._logged_in = True
        recovered = mock.Mock(return_value="works-paid-once")
        generated = mock.Mock(side_effect=AssertionError("不能重新提交"))
        session._recover_works_id_by_name = recovered
        session._generate_pending_one = generated
        session._download_pending_batch = lambda pending: {
            item["works_id"]: {**item, "downloaded": True}
            for item in pending
        }

        result = session.synth_batch([{
            "job_id": "resume-ambiguous",
            "text": "hello",
            "voice_key": "amanda",
            "works_name": "wordtts_paid_once",
            "ambiguous_works_name": "wordtts_paid_once",
        }])

        generated.assert_not_called()
        recovered.assert_called_once()
        self.assertTrue(result["resume-ambiguous"]["downloaded"])
        self.assertEqual(result["resume-ambiguous"]["works_id"], "works-paid-once")

    def test_composite_reconciles_ambiguous_name_without_submitting_again(self):
        session = XunFeiSession()
        session._logged_in = True
        recovered = mock.Mock(return_value="composite-paid-once")
        generated = mock.Mock(side_effect=AssertionError("不能重新提交多人作品"))
        session._recover_works_id_by_name = recovered
        session._generate_pending_composite = generated
        session._download_pending_batch = lambda pending, **_kwargs: {
            item["works_id"]: {**item, "downloaded": True}
            for item in pending
        }

        result = session.synth_composite(
            [{
                "work_id": "composite-resume-ambiguous",
                "works_name": "wordtts_composite_paid",
                "items": [],
                "item_count": 1,
            }],
            resume={
                "composite-resume-ambiguous": {
                    "works_name": "wordtts_composite_paid",
                    "ambiguous_submission": True,
                },
            },
        )

        generated.assert_not_called()
        recovered.assert_called_once()
        self.assertTrue(result["composite-resume-ambiguous"]["downloaded"])
        self.assertEqual(
            result["composite-resume-ambiguous"]["works_id"],
            "composite-paid-once",
        )

    def test_batch_cancel_check_stops_before_next_submission(self):
        session = XunFeiSession()
        session._logged_in = True
        generated = []

        def fake_generate(text, **_kwargs):
            generated.append(text)
            return {
                "works_id": f"works-{len(generated)}",
                "output_path": f"/tmp/works-{len(generated)}.mp3",
            }

        session._generate_pending_one = fake_generate
        session._download_pending_batch = lambda pending, **_kwargs: {}

        self.assertRaises(
            xunfei.XunfeiCancelled,
            session.synth_batch,
            [
                {"job_id": "first", "text": "first", "voice_key": "amanda"},
                {"job_id": "second", "text": "second", "voice_key": "amanda"},
            ],
            cancel_check=lambda: len(generated) >= 1,
        )
        self.assertEqual(generated, ["first"])

    def test_composite_rate_limit_recovery_reloads_page_before_retry(self):
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page
        work = {
            "work_id": "composite:rate-limit",
            "works_name": "rate-limit-test",
            "item_ids": ["q1"],
            "item_count": 1,
        }

        with mock.patch.object(session, "_prepare_composite_editor"), \
                mock.patch.object(session, "_mark_works_cutoff"), \
                mock.patch.object(session, "_click_generate"), \
                mock.patch.object(
                    session,
                    "_confirm_synth",
                    side_effect=["rate_limited", "ok"],
                ), \
                mock.patch.object(session, "_consume_works_id", return_value="final-id"), \
                mock.patch.object(session, "_cleanup_after_item"), \
                mock.patch.object(session, "_recover_and_retry", return_value=True) as recover:
            pending = session._generate_pending_composite(
                work,
                max_retries=2,
            )

        recover.assert_called_once_with(page)
        self.assertEqual(pending["works_id"], "final-id")

    def test_works_id_consumer_prefers_final_order_id_over_temporary_id(self):
        class FakeRequest:
            post_data_json = None

            @staticmethod
            def all_headers():
                return {}

        class FakeResponse:
            def __init__(self, url, payload):
                self.url = url
                self.request = FakeRequest()
                self._payload = payload

            def json(self):
                return self._payload

        session = XunFeiSession()
        session._mark_works_cutoff()
        session._on_response(FakeResponse(
            "https://example.test/makeMultipleSpeakerWork",
            {"retCode": 0, "tempWorksId": "temporary-id"},
        ))
        session._on_response(FakeResponse(
            "https://example.test/order_gen",
            {
                "code": 0,
                "data": {"payOrder": {"worksId": "final-id"}},
            },
        ))

        self.assertEqual(session._consume_works_id(timeout=0.05), "final-id")

    def test_works_id_consumer_waits_for_final_id_when_temporary_arrives_first(self):
        class FakeRequest:
            post_data_json = None

            @staticmethod
            def all_headers():
                return {}

        class FakeResponse:
            def __init__(self, url, payload):
                self.url = url
                self.request = FakeRequest()
                self._payload = payload

            def json(self):
                return self._payload

        session = XunFeiSession()
        session._mark_works_cutoff()
        session._on_response(FakeResponse(
            "https://example.test/makeMultipleSpeakerWork",
            {"retCode": 0, "tempWorksId": "temporary-id"},
        ))

        def emit_final_id():
            # 覆盖旧的 0.8s grace window，模拟网络稍慢但仍属于同一次提交。
            time.sleep(1.0)
            session._on_response(FakeResponse(
                "https://example.test/order_gen",
                {
                    "code": 0,
                    "data": {"payOrder": {"worksId": "final-id"}},
                },
            ))

        emitter = threading.Thread(target=emit_final_id)
        emitter.start()
        try:
            self.assertEqual(session._consume_works_id(timeout=1.2), "final-id")
        finally:
            emitter.join(timeout=1)

    def test_submission_response_fence_ignores_delayed_old_request(self):
        """上一条提交的延迟 response 不能串到下一条作品。"""
        class FakeRequest:
            post_data_json = None

            def __init__(self, impl, url):
                self._impl_obj = impl
                self.url = url

            @staticmethod
            def all_headers():
                return {}

        class FakeResponse:
            def __init__(self, request, payload):
                self.url = request.url
                self.request = request
                self._payload = payload

            def json(self):
                return self._payload

        session = XunFeiSession()
        old_impl = object()
        session._on_request(FakeRequest(old_impl, "https://example.test/order_gen"))
        session._mark_works_cutoff()

        # response.request 使用另一个 Python 包装对象，但底层实现对象相同。
        session._on_response(FakeResponse(
            FakeRequest(old_impl, "https://example.test/order_gen"),
            {"code": 0, "data": {"payOrder": {"worksId": "old-id"}}},
        ))
        self.assertIsNone(session._consume_works_id(timeout=0.02))

        new_impl = object()
        session._on_request(FakeRequest(new_impl, "https://example.test/order_gen"))
        session._on_response(FakeResponse(
            FakeRequest(new_impl, "https://example.test/order_gen"),
            {"code": 0, "data": {"payOrder": {"worksId": "new-id"}}},
        ))
        self.assertEqual(session._consume_works_id(timeout=0.05), "new-id")

    def test_works_id_fallback_can_exclude_temporary_multi_speaker_id(self):
        session = XunFeiSession()
        now = time.time()
        with session._works_lock:
            session._works_cutoff = now - 1
            session._works_entries = [("temporary-id", now)]

        self.assertIsNone(
            session._consume_works_id(
                timeout=0.02,
                exclude_ids={"temporary-id"},
            )
        )

    def test_text_input_is_a_single_paste_like_insert(self):
        page = _FakePage()
        text = "Reporter: " + ("This is a long sentence. " * 40)

        self.assertTrue(XunFeiSession._type_text(page, text))
        self.assertEqual(page.keyboard.inserted, [text])
        self.assertEqual(page.keyboard.typed, [])

    def test_voice_cache_is_invalidated_when_xunfei_resets_to_default_voice(self):
        """提交上一条作品后页面复位时，下一条不能盲信本地音色缓存。"""
        from playwright.sync_api import sync_playwright

        html = """
        <input class="h-full w-full" placeholder="搜索主播 / 标签">
        <div id="voices">
          <button type="button" class="voice-card is-selected" aria-selected="true">
            <p>Amanda</p><span>女声</span>
          </button>
          <button type="button" class="voice-card" aria-selected="false">
            <p>Linda-品质</p><span>女声</span>
          </button>
        </div>
        <script>
          for (const button of document.querySelectorAll('.voice-card')) {
            button.addEventListener('click', () => {
              for (const other of document.querySelectorAll('.voice-card')) {
                other.classList.remove('is-selected');
                other.setAttribute('aria-selected', 'false');
              }
              button.classList.add('is-selected');
              button.setAttribute('aria-selected', 'true');
            });
          }
        </script>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            session = XunFeiSession()
            session._current_voice_key = "speaker:linda"
            session._current_voice_name = "Linda-品质"
            session._applied_params = {"speed": 50, "pitch": 50, "volume": 50}

            self.assertTrue(
                session._select_voice(
                    page,
                    "Linda-品质",
                    voice_key="speaker:linda",
                )
            )
            self.assertEqual(
                page.locator('input[placeholder*="搜索"]').input_value(),
                "Linda-品质",
            )
            self.assertEqual(
                page.evaluate(xunfei.JS.CHECK_VOICE_SELECTED, "Linda-品质"),
                "Linda-品质女声",
            )
            self.assertIsNone(session._applied_params)
            browser.close()

    def test_existing_sync_session_is_checked_off_the_asyncio_loop(self):
        original_session = xunfei_runtime._session
        original_available = xunfei_runtime.is_available
        original_health = xunfei_runtime._session_is_healthy
        seen_threads = []
        fake_session = _HealthyFakeSession()
        xunfei_runtime._session = fake_session
        xunfei_runtime.is_available = lambda: True

        def health_check(session):
            seen_threads.append(threading.current_thread())
            return session is fake_session

        xunfei_runtime._session_is_healthy = health_check
        try:
            result = asyncio.run(xunfei.ensure_session())
        finally:
            xunfei_runtime._session = original_session
            xunfei_runtime.is_available = original_available
            xunfei_runtime._session_is_healthy = original_health

        self.assertIs(result, fake_session)
        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(seen_threads[0], threading.current_thread())

    def test_all_playwright_sync_calls_share_one_dedicated_thread(self):
        seen_threads = []

        def record_thread():
            seen_threads.append(threading.current_thread())

        async def run_calls():
            await xunfei_runtime._run_playwright_sync(record_thread)
            await xunfei_runtime._run_playwright_sync(record_thread)

        asyncio.run(run_calls())

        self.assertEqual(len(seen_threads), 2)
        self.assertIs(seen_threads[0], seen_threads[1])
        self.assertIsNot(seen_threads[0], threading.current_thread())

    def test_ai_modal_has_priority_over_rate_limit_status(self):
        session = XunFeiSession()
        page = _PostConfirmPage(ai_modal=True, rate_limited=True)

        self.assertEqual(session._observe_after_first_confirm(page), "ai_modal")

    def test_delayed_ai_modal_is_captured_after_page_renders(self):
        """页面延迟挂载 AI 弹窗时，轮询仍能捕获，不依赖一次性查询。"""
        from playwright.sync_api import sync_playwright

        html = (
            '<div class="ant-modal" role="dialog" '
            'style="display:block;width:320px;height:120px">'
            'AI 标识说明 不再提示</div>'
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            # 真实 DOM 探针单独验证；下面的 DelayedProbePage 再验证首次
            # 未命中、后续页面才出现时轮询仍会继续捕获。
            self.assertEqual(
                page.evaluate(
                    xunfei.JS.PROBE_SYNTH_STATE,
                    xunfei.AI_FLAG_KEYWORD_VARIANTS,
                )["state"],
                "ai_modal",
            )
            self.assertEqual(
                XunFeiSession()._observe_after_first_confirm(page),
                "ai_modal",
            )
            browser.close()

        class DelayedProbePage:
            def __init__(self):
                self.calls = 0

            def evaluate(self, script, arg=None):
                if script != xunfei.JS.PROBE_SYNTH_STATE:
                    return None
                self.calls += 1
                if self.calls == 1:
                    return {"state": None, "ai_modal": False, "ai_switch": "not_found"}
                return {"state": "ai_modal", "ai_modal": True, "ai_switch": "not_found"}

            def wait_for_timeout(self, _milliseconds):
                return None

        delayed_page = DelayedProbePage()
        self.assertEqual(
            XunFeiSession()._observe_after_first_confirm(delayed_page),
            "ai_modal",
        )
        self.assertGreaterEqual(delayed_page.calls, 2)

    def test_interrupted_xunfei_draft_prompt_is_dismissed_before_reuse(self):
        """中断后复用持久浏览器时，不能让讯飞本地缓存提示拦住新任务。"""
        from playwright.sync_api import sync_playwright

        html = """
        <div id="draft-prompt" style="display:block;width:420px;height:180px">
          <strong>发现本地缓存</strong>
          <p>检测到上一次编辑内容，是否恢复？</p>
          <button id="blank" type="button"
                  onclick="document.getElementById('draft-prompt').remove()">
            空白开始
          </button>
          <button type="button">恢复本地缓存</button>
        </div>
        <div class="ssml-editor" contenteditable="true"></div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            session = XunFeiSession()

            self.assertTrue(session._dismiss_local_draft_prompt(page, timeout=1))
            self.assertEqual(page.locator("#draft-prompt").count(), 0)
            self.assertEqual(page.locator(".ssml-editor").count(), 1)
            browser.close()

    def test_order_modal_does_not_wait_for_missing_ai_switch_again(self):
        """订单支付弹窗出现后，不应再次等待不存在的作品设置开关。"""
        session = XunFeiSession()
        ai_checks = []

        class FakePage:
            def evaluate(self, script, arg=None):
                if script == xunfei.JS.CHECK_MODAL_HAS_TEXT:
                    return arg == ["确认合成"]
                return False

            def wait_for_timeout(self, _milliseconds):
                return None

        session._visible_confirm_synth_buttons = lambda _page: [(object(), False)]
        session._ensure_mp3_format = lambda _page: True
        session._ensure_ai_switch_off = (
            lambda _page, timeout=8: ai_checks.append(timeout) or "off"
        )
        session._click_confirm_synth_button = lambda _page: True
        session._observe_after_first_confirm = lambda _page: "order"

        self.assertEqual(session._confirm_synth(FakePage()), "ok")
        self.assertEqual(ai_checks, [12])

    def test_ai_switch_logic_requires_an_off_state(self):
        self.assertIn("return 'already_off'", xunfei.JS.CLICK_AI_SWITCH)
        self.assertIn("return 'clicked'", xunfei.JS.CLICK_AI_SWITCH)
        self.assertIn("CHECK_AI_SWITCH_OFF", dir(xunfei.JS))

        session = XunFeiSession()

        class FakePage:
            def evaluate(self, script, arg=None):
                if script == xunfei.JS.PROBE_SYNTH_STATE:
                    return {
                        "state": "confirm",
                        "ai_modal": False,
                        "ai_switch": "off",
                    }
                if script == xunfei.JS.CHECK_NO_REMIND:
                    return "clicked_input"
                if script == xunfei.JS.CLICK_AI_SWITCH:
                    return "clicked"
                if script == xunfei.JS.GET_AI_SWITCH_STATE:
                    return "off"
                if script == xunfei.JS.CHECK_AI_SWITCH_OFF:
                    return True
                if script == xunfei.JS.CLICK_AI_CONFIRM:
                    return True
                if script == xunfei.JS.CHECK_MODAL_HAS_TEXT:
                    return False
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

        self.assertTrue(session._handle_ai_flag_dialog(FakePage()))

    def test_ai_switch_matches_the_real_ant_modal_dom(self):
        from playwright.sync_api import sync_playwright

        html = """
        <div role="dialog" class="ant-modal">
          <div class="ant-modal-content">
            <div class="ant-modal-title">作品设置</div>
            <div class="flex items-center justify-between">
              <span>AI 标识</span>
              <button type="button" role="switch" aria-checked="true"
                      class="ant-switch ant-switch-checked">
                <div class="ant-switch-handle"></div>
              </button>
            </div>
            <div class="flex items-center justify-between">
              <span>其它设置</span>
              <button type="button" role="switch" aria-checked="false"
                      class="ant-switch"></button>
            </div>
            <button>确认合成</button>
          </div>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            page.evaluate(
                """() => {
                    const switchButton = document.querySelector('[role=switch]');
                    switchButton.addEventListener('click', () => {
                        switchButton.setAttribute('aria-checked', 'false');
                        switchButton.classList.remove('ant-switch-checked');
                    });
                }"""
            )
            probe = page.evaluate(
                xunfei.JS.PROBE_SYNTH_STATE,
                xunfei.AI_FLAG_KEYWORD_VARIANTS,
            )
            self.assertEqual(probe["state"], "confirm")
            self.assertEqual(probe["ai_switch"], "on")
            self.assertEqual(page.evaluate(xunfei.JS.GET_AI_SWITCH_STATE), "on")
            self.assertEqual(page.evaluate(xunfei.JS.CLICK_AI_SWITCH), "clicked")
            page.wait_for_timeout(30)
            self.assertEqual(
                page.evaluate(
                    xunfei.JS.PROBE_SYNTH_STATE,
                    xunfei.AI_FLAG_KEYWORD_VARIANTS,
                )["ai_switch"],
                "off",
            )
            self.assertEqual(page.evaluate(xunfei.JS.GET_AI_SWITCH_STATE), "off")
            browser.close()

    def test_export_format_selects_mp3_by_real_radio_label(self):
        """验证真实“作品设置”DOM 从 WAV 默认值切换到 MP3。"""
        from playwright.sync_api import sync_playwright

        html = """
        <div role="dialog" aria-modal="true" class="ant-modal" style="width: 420px">
          <div class="ant-modal-content">
            <div class="ant-modal-header"><div class="ant-modal-title">作品设置</div></div>
            <div class="flex flex-col gap-1">
              <span>格式</span>
              <div class="flex gap-3">
                <label><input type="radio" name="exportFormat" value="wav" checked><span>WAV</span></label>
                <label><input type="radio" name="exportFormat"><span>MP3</span></label>
              </div>
            </div>
            <button>确认合成</button>
          </div>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)

            self.assertEqual(page.evaluate(xunfei.JS.GET_MP3_FORMAT)["status"], "other")
            result = page.evaluate(xunfei.JS.SET_MP3_FORMAT)
            self.assertEqual(result["status"], "clicked_mp3")
            self.assertTrue(page.evaluate(xunfei.JS.GET_MP3_FORMAT)["checked"])
            self.assertTrue(page.locator('input[value="wav"]').is_checked() is False)

            # 再把页面恢复成 Windows 端可能出现的 WAV 默认值，验证 Python
            # 流程会在最终确认前强制切换并回读 MP3。
            page.locator('input[value="wav"]').check()
            self.assertTrue(XunFeiSession()._ensure_mp3_format(page, timeout=2))
            self.assertTrue(page.evaluate(xunfei.JS.GET_MP3_FORMAT)["checked"])
            browser.close()

    def test_missing_mp3_never_falls_back_to_wav(self):
        """MP3 元素缺失时必须中止，不能把 WAV 当成默认项点击。"""
        from playwright.sync_api import sync_playwright

        html = """
        <div role="dialog" class="ant-modal" style="width: 420px">
          <div class="ant-modal-content">
            <div class="ant-modal-title">作品设置</div>
            <label><input type="radio" name="exportFormat" value="wav" checked><span>WAV</span></label>
            <button>确认合成</button>
          </div>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            self.assertEqual(
                page.evaluate(xunfei.JS.SET_MP3_FORMAT)["status"],
                "mp3_not_found",
            )
            self.assertTrue(page.locator('input[value="wav"]').is_checked())
            self.assertFalse(XunFeiSession()._ensure_mp3_format(page, timeout=1))
            self.assertTrue(page.locator('input[value="wav"]').is_checked())
            browser.close()

    def test_ai_info_modal_checks_no_remind_and_confirms(self):
        """验证用户提供的 Ant Design AI 标识说明弹窗 DOM。"""
        from playwright.sync_api import sync_playwright

        html = """
        <div class="ant-modal" role="dialog" aria-modal="true">
          <div class="ant-modal-content">
            <button type="button" aria-label="Close" class="ant-modal-close">
              <span class="ant-modal-close-x" aria-label="Close">×</span>
            </button>
            <div class="ant-modal-header">
              <div class="ant-modal-title">AI 标识说明</div>
            </div>
            <div class="ant-modal-body">
              <p>根据相关法规要求，使用 AI 技术合成的音频将在开头添加 AI 声明水印。</p>
              <p>您可以在作品设置中关闭 AI 标识开关来取消水印。</p>
            </div>
            <div class="ant-modal-footer">
              <label class="ant-checkbox-wrapper">
                <span class="ant-checkbox">
                  <input class="ant-checkbox-input" type="checkbox">
                  <span class="ant-checkbox-inner"></span>
                </span>
                <span class="ant-checkbox-label"><span>不再提示</span></span>
              </label>
              <button>取消</button>
              <button id="confirm">确认</button>
            </div>
          </div>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            page.evaluate(
                """() => {
                    document.querySelector('#confirm').addEventListener('click', () => {
                        document.querySelector('.ant-modal').remove();
                    });
                }"""
            )

            self.assertEqual(page.evaluate(xunfei.JS.CHECK_NO_REMIND), "clicked_input")
            self.assertTrue(page.locator('input.ant-checkbox-input').is_checked())
            self.assertTrue(page.evaluate(xunfei.JS.CLICK_AI_CONFIRM))
            self.assertEqual(page.locator('.ant-modal').count(), 0)
            browser.close()

    def test_download_rows_use_order_no_when_names_are_duplicate(self):
        """下载页两个同名作品必须按订单号选择对应行。"""
        from playwright.sync_api import sync_playwright

        html = """
        <div class="header">
          <input class="ant-checkbox-input" type="checkbox">
        </div>
        <div class="index-module__scrolledList">
          <div class="index-module__item">
            <div class="index-module__checkbox">
              <input class="ant-checkbox-input" type="checkbox">
            </div>
            <div class="index-module__name">同名作品</div>
            <div>订单编号: PO-FIRST</div>
          </div>
          <div class="index-module__item">
            <div class="index-module__checkbox">
              <input class="ant-checkbox-input" type="checkbox">
            </div>
            <div class="index-module__name">同名作品</div>
            <div>订单编号: PO-SECOND</div>
          </div>
        </div>
        """
        targets = [
            {"works_id": "works-second", "order_no": "PO-SECOND", "works_name": "同名作品"},
            {"works_id": "works-first", "order_no": "PO-FIRST", "works_name": "同名作品"},
        ]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            state = page.evaluate(xunfei.JS.SELECT_DOWNLOAD_ROWS, targets)

            self.assertEqual(state["row_count"], 2)
            self.assertEqual(
                [item["works_id"] for item in state["selected"]],
                ["works-second", "works-first"],
            )
            self.assertEqual(state["missing"], [])
            self.assertEqual(
                page.locator('input.ant-checkbox-input:checked').count(),
                2,
            )
            browser.close()

    def test_download_uses_current_xunfei_user_page(self):
        self.assertEqual(xunfei.DOWNLOAD_PAGE_URL, "https://peiyin.xunfei.cn/user")
        self.assertIn("order_no", xunfei.JS.SELECT_DOWNLOAD_ROWS)
        self.assertIn("input.ant-checkbox-input", xunfei.JS.SELECT_DOWNLOAD_ROWS)

    def test_rate_limit_script_uses_visible_text_only(self):
        self.assertIn("body.innerText", xunfei.JS.CHECK_RATE_LIMITED)
        self.assertNotIn("body.textContent", xunfei.JS.CHECK_RATE_LIMITED)

    def test_video_api_sign_matches_xunfei_browser_rule(self):
        param = {"needCount": 1, "pageIndex": 1, "pageSize": 30, "worksName": ""}
        base = {
            "appid": "xfpy",
            "sid": "GHdKT2aJFRTYaTYbndgJjlkEm1FCk8db",
            "channelId": "40000001",
            "userId": "260825114234318730",
            "osid": 0,
        }

        self.assertEqual(
            xunfei._build_api_sign(param, base),
            "c9b3e6ea75dccb702f69104c1d94d771",
        )

    def test_works_id_matching_is_exact(self):
        session = XunFeiSession()

        class FakePage:
            def wait_for_timeout(self, _milliseconds):
                return None

        session._fetch_works_list_in_page = lambda _page, needed_count=1: [
            {"id": "other-id", "worksName": "same name"},
            {"id": "target-id", "worksName": "same name"},
        ]

        result = session._wait_for_works_entry(FakePage(), "target-id", timeout=0.1)
        self.assertEqual(result["id"], "target-id")

    def test_batch_groups_same_voice_and_parameters_before_generating(self):
        session = XunFeiSession()
        session._logged_in = True
        generated = []
        download_batches = []

        def fake_generate(text, **kwargs):
            generated.append((text, kwargs["voice_key"], kwargs["speed"], kwargs["pitch"], kwargs["volume"]))
            works_id = f"works-{len(generated)}"
            return {
                "works_id": works_id,
                "output_path": f"/tmp/{works_id}.mp3",
            }

        def fake_download(pending):
            download_batches.append(list(pending))
            return {
                item["works_id"]: {**item, "downloaded": True}
                for item in pending
            }

        session._generate_pending_one = fake_generate
        session._download_pending_batch = fake_download
        jobs = [
            {"job_id": "g1", "text": "George one", "voice_key": "george", "speed": 50, "pitch": 50, "volume": 50},
            {"job_id": "a1", "text": "Amanda one", "voice_key": "amanda", "speed": 35, "pitch": 50, "volume": 50},
            {"job_id": "a2", "text": "Amanda two", "voice_key": "amanda", "speed": 35, "pitch": 50, "volume": 50},
        ]

        result = session.synth_batch(jobs)

        self.assertEqual(
            [item[0] for item in generated],
            ["George one", "Amanda one", "Amanda two"],
        )
        self.assertEqual(len(download_batches), 1)
        self.assertEqual(len(download_batches[0]), 3)
        self.assertTrue(all(result[job["job_id"]]["downloaded"] for job in jobs))

    def test_batch_keeps_multiple_non_default_voice_keys(self):
        """多角色批量任务不能把自定义音色回退为 Amanda/George。"""
        xunfei.register_voice_catalog([
            {"key": "speaker:test-linda", "name": "Linda-品质", "gender": "female"},
            {"key": "speaker:test-catherine", "name": "Catherine-品质", "gender": "female"},
            {"key": "speaker:test-steve", "name": "Steve", "gender": "male"},
        ])
        session = XunFeiSession()
        session._logged_in = True
        generated = []

        def fake_generate(text, **kwargs):
            generated.append((text, kwargs["voice_key"]))
            works_id = f"works-{len(generated)}"
            return {"works_id": works_id, "output_path": f"/tmp/{works_id}.mp3"}

        session._generate_pending_one = fake_generate
        session._download_pending_batch = lambda pending: {
            item["works_id"]: {**item, "downloaded": True}
            for item in pending
        }
        jobs = [
            {"job_id": "linda", "text": "Linda", "voice_key": "speaker:test-linda"},
            {"job_id": "catherine", "text": "Catherine", "voice_key": "speaker:test-catherine"},
            {"job_id": "steve", "text": "Steve", "voice_key": "speaker:test-steve"},
        ]

        result = session.synth_batch(jobs)

        self.assertEqual(
            [voice_key for _text, voice_key in generated],
            [job["voice_key"] for job in jobs],
        )
        self.assertTrue(all(result[job["job_id"]]["downloaded"] for job in jobs))

    def test_batch_reports_download_and_save_progress(self):
        session = XunFeiSession()
        session._logged_in = True
        events = []

        def fake_generate(text, **kwargs):
            return {
                "works_id": f"works-{text}",
                "output_path": f"/tmp/{text}.mp3",
            }

        def fake_download(pending, progress_callback=None):
            for item in pending:
                progress_callback({
                    "job_id": item["job_id"],
                    "downloaded": True,
                    "stage": "downloaded",
                })
                progress_callback({
                    "job_id": item["job_id"],
                    "downloaded": True,
                    "stage": "saved",
                })
            return {
                item["works_id"]: {**item, "downloaded": True}
                for item in pending
            }

        session._generate_pending_one = fake_generate
        session._download_pending_batch = fake_download
        jobs = [
            {
                "job_id": "batch-1",
                "text": "first",
                "voice_key": "amanda",
                "speed": 50,
                "pitch": 50,
                "volume": 50,
            },
            {
                "job_id": "batch-2",
                "text": "second",
                "voice_key": "amanda",
                "speed": 50,
                "pitch": 50,
                "volume": 50,
            },
        ]

        result = session.synth_batch(jobs, progress_callback=events.append)

        self.assertEqual(
            [(event["job_id"], event["stage"]) for event in events],
            [
                ("batch-1", "submitted"),
                ("batch-2", "submitted"),
                ("batch-1", "downloaded"),
                ("batch-1", "saved"),
                ("batch-2", "downloaded"),
                ("batch-2", "saved"),
            ],
        )
        self.assertTrue(all(result[job["job_id"]]["downloaded"] for job in jobs))

    def test_batch_download_exception_returns_each_submitted_works_id(self):
        """统一下载页异常时也要逐条返回，供断点重试复用作品。"""
        session = XunFeiSession()
        session._logged_in = True
        generated = []
        events = []

        def fake_generate(text, **_kwargs):
            works_id = f"works-{len(generated) + 1}"
            generated.append(text)
            return {
                "works_id": works_id,
                "output_path": f"/tmp/{works_id}.mp3",
            }

        def fail_download(_pending, **_kwargs):
            raise xunfei.XunfeiError("下载页暂时不可用")

        session._generate_pending_one = fake_generate
        session._download_pending_batch = fail_download
        jobs = [
            {"job_id": "failed-1", "text": "first", "voice_key": "amanda"},
            {"job_id": "failed-2", "text": "second", "voice_key": "amanda"},
        ]

        result = session.synth_batch(jobs, progress_callback=events.append)

        self.assertEqual(generated, ["first", "second"])
        self.assertEqual(
            {job_id: result[job_id]["works_id"] for job_id in result},
            {"failed-1": "works-1", "failed-2": "works-2"},
        )
        self.assertTrue(all(not item["downloaded"] for item in result.values()))
        self.assertTrue(all("统一下载异常" in item["error"] for item in result.values()))
        self.assertEqual(
            [(event["job_id"], event["stage"], event.get("works_id")) for event in events],
            [
                ("failed-1", "submitted", "works-1"),
                ("failed-2", "submitted", "works-2"),
                ("failed-1", "saved", "works-1"),
                ("failed-2", "saved", "works-2"),
            ],
        )

    def test_batch_marks_missing_works_id_as_invalid_only_after_reliable_scan(self):
        session = XunFeiSession()
        session._logged_in = True
        session._page = mock.Mock()
        session._last_works_list_scan_complete = True
        pending = [{
            "job_id": "missing-job",
            "works_id": "missing-works",
            "works_name": "missing",
            "output_path": "/tmp/missing.mp3",
        }]

        with mock.patch.object(session, "_wait_for_pending_ready", return_value={}), \
                mock.patch.object(session, "_fetch_works_list_pages", return_value=[]), \
                mock.patch.object(session, "_fetch_sign_url_in_page", return_value=None), \
                mock.patch.object(xunfei_downloads, "_safe_eval", return_value=True):
            result = session._download_pending_batch(pending)

        self.assertTrue(result["missing-works"]["works_id_invalid"])
        self.assertIn("失效", result["missing-works"]["error"])

    def test_batch_reuses_submitted_works_id_without_submitting_again(self):
        session = XunFeiSession()
        session._logged_in = True
        generated = []
        downloads = []

        def should_not_generate(text, **_kwargs):
            generated.append(text)
            raise AssertionError("断点重试不应再次提交讯飞合成")

        def fake_download(pending):
            downloads.append(list(pending))
            return {
                item["works_id"]: {**item, "downloaded": True}
                for item in pending
            }

        session._generate_pending_one = should_not_generate
        session._download_pending_batch = fake_download
        result = session.synth_batch([{
            "job_id": "resume-1",
            "text": "same text",
            "voice_key": "amanda",
            "resume_works_id": "works-already-paid",
        }])

        self.assertEqual(generated, [])
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0][0]["works_id"], "works-already-paid")
        self.assertTrue(result["resume-1"]["downloaded"])

    def test_batch_rejects_duplicate_works_ids_without_download(self):
        session = XunFeiSession()
        session._logged_in = True
        download_calls = []

        def fake_generate(_text, **_kwargs):
            return {"works_id": "same-works", "output_path": "/tmp/same.mp3"}

        def fake_download(pending, **_kwargs):
            download_calls.append(list(pending))
            return {}

        session._generate_pending_one = fake_generate
        session._download_pending_batch = fake_download
        result = session.synth_batch([
            {"job_id": "duplicate-1", "text": "one", "voice_key": "amanda"},
            {"job_id": "duplicate-2", "text": "two", "voice_key": "amanda"},
        ])

        self.assertEqual(download_calls, [[]])
        for job_id in ("duplicate-1", "duplicate-2"):
            self.assertFalse(result[job_id]["downloaded"])
            self.assertTrue(result[job_id]["ambiguous_works_id"])
            self.assertIn("重复", result[job_id]["error"])

    def test_composite_rejects_duplicate_works_ids_without_download(self):
        session = XunFeiSession()
        session._logged_in = True
        download_calls = []

        def fake_generate(work, **_kwargs):
            return {
                "works_id": "same-composite-works",
                "output_path": f"/tmp/{work['work_id']}.mp3",
                "works_name": work.get("works_name", "composite"),
            }

        def fake_download(pending, **_kwargs):
            download_calls.append(list(pending))
            return {}

        session._generate_pending_composite = fake_generate
        session._download_pending_batch = fake_download
        result = session.synth_composite([
            {"work_id": "composite-1", "items": []},
            {"work_id": "composite-2", "items": []},
        ])

        self.assertEqual(download_calls, [[]])
        for work_id in ("composite-1", "composite-2"):
            self.assertFalse(result[work_id]["downloaded"])
            self.assertTrue(result[work_id]["ambiguous_works_id"])
            self.assertIn("重复", result[work_id]["error"])

    def test_composite_reports_confirmed_ambiguous_submit_without_retry(self):
        session = XunFeiSession()
        session._logged_in = True
        generated = []
        events = []

        def ambiguous_generate(_work, **_kwargs):
            generated.append(True)
            raise xunfei.XunfeiSubmissionAmbiguous(
                "已确认提交但未捕获 worksId",
                works_name="wordtts_composite_paid",
            )

        session._generate_pending_composite = ambiguous_generate
        session._download_pending_batch = lambda pending, **_kwargs: {}
        result = session.synth_composite([{
            "work_id": "composite-ambiguous",
            "works_name": "wordtts_composite_paid",
            "items": [],
            "item_count": 1,
        }], progress_callback=events.append)

        self.assertEqual(generated, [True])
        self.assertTrue(result["composite-ambiguous"]["ambiguous_works_id"])
        self.assertEqual(
            result["composite-ambiguous"]["works_name"],
            "wordtts_composite_paid",
        )
        self.assertTrue(any(event.get("ambiguous_works_id") for event in events))

    def test_pending_ready_scans_later_works_list_pages(self):
        session = XunFeiSession()
        calls = []

        class FakePage:
            def wait_for_timeout(self, _milliseconds):
                return None

        def fake_fetch(_page, needed_count=1, page_index=1):
            calls.append((needed_count, page_index))
            if page_index == 1:
                return [{"id": "newer", "worksName": "other"}]
            if page_index == 2:
                return [{
                    "id": "target",
                    "worksName": "target-name",
                    "audioUrl": "https://example.test/target.mp3",
                }]
            return []

        session._fetch_works_list_in_page = fake_fetch
        ready = session._wait_for_pending_ready(
            FakePage(),
            [{"works_id": "target", "job_id": "job-target", "output_path": "/tmp/target.mp3"}],
            timeout=0.1,
        )

        self.assertEqual(ready["target"]["download_url"], "https://example.test/target.mp3")
        self.assertEqual([page_index for _needed, page_index in calls], [1, 2])

    def test_browser_download_matching_does_not_use_arrival_order(self):
        class FakeDownload:
            def __init__(self, filename):
                self.suggested_filename = filename

        targets = [
            {
                "works_id": "works-a",
                "works_name": "wordtts_0001_a1b2c3d4",
                "item": {},
            },
            {
                "works_id": "works-b",
                "works_name": "wordtts_0002_e5f6g7h8",
                "item": {},
            },
        ]
        downloads = [
            FakeDownload("wordtts_0002_e5f6g7h8.mp3"),
            FakeDownload("wordtts_0001_a1b2c3d4.mp3"),
        ]

        first_index = XunFeiSession._match_download_index(downloads, targets[0])
        first_download = downloads.pop(first_index)
        second_index = XunFeiSession._match_download_index(downloads, targets[1])

        self.assertEqual(first_download.suggested_filename, "wordtts_0001_a1b2c3d4.mp3")
        self.assertEqual(downloads[second_index].suggested_filename, "wordtts_0002_e5f6g7h8.mp3")

    def test_browser_download_does_not_assign_one_unknown_file_to_first_of_two_targets(self):
        class FakeDownload:
            suggested_filename = "wordtts_0002_e5f6g7h8.mp3"

            def save_as(self, output_path):
                Path(output_path).write_bytes(b"ID3\x04")

        session = XunFeiSession()
        session._page = mock.Mock()
        pending = [
            {
                "job_id": "job-a",
                "works_id": "works-a",
                "works_name": "wordtts_0001_a1b2c3d4",
                "output_path": "",
            },
            {
                "job_id": "job-b",
                "works_id": "works-b",
                "works_name": "wordtts_0002_e5f6g7h8",
                "output_path": "",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            pending[0]["output_path"] = str(Path(temp_dir, "a.mp3"))
            pending[1]["output_path"] = str(Path(temp_dir, "b.mp3"))
            ready = {
                "works-a": {"record": {"id": "works-a"}},
                "works-b": {"record": {"id": "works-b"}},
            }
            records = [
                {"id": "works-a", "worksName": pending[0]["works_name"]},
                {"id": "works-b", "worksName": pending[1]["works_name"]},
            ]
            with mock.patch.object(session, "_wait_for_pending_ready", return_value=ready), \
                    mock.patch.object(session, "_fetch_works_list_in_page", return_value=records), \
                    mock.patch.object(session, "_download_signed_url", return_value=False), \
                    mock.patch.object(
                        session,
                        "_select_download_rows",
                        return_value=({"works-a", "works-b"}, []),
                    ), \
                    mock.patch.object(
                        session,
                        "_download_selected_rows",
                        return_value=[FakeDownload()],
                    ), \
                        mock.patch.object(xunfei_downloads, "_safe_eval", return_value=True):
                results = session._download_pending_batch(pending)

            self.assertFalse(results["works-a"]["downloaded"])
            self.assertTrue(results["works-b"]["downloaded"])
            self.assertFalse(Path(pending[0]["output_path"]).exists())
            self.assertTrue(Path(pending[1]["output_path"]).exists())

    def test_signed_url_download_writes_only_valid_mp3(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                if size == -1:
                    return b"ID3\x04"
                if not hasattr(self, "sent"):
                    self.sent = True
                    return b"ID3\x04"
                return b""

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = str(Path(temp_dir, "audio.mp3"))
            with mock.patch.object(
                xunfei_downloads.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ):
                self.assertTrue(
                    XunFeiSession._download_signed_url(
                        "https://example.test/signed.mp3",
                        output_path,
                    )
                )
            self.assertEqual(Path(output_path).read_bytes(), b"ID3\x04")
            self.assertFalse(Path(f"{output_path}.part").exists())

    def test_cleanup_clears_editor_without_navigation(self):
        session = XunFeiSession()
        calls = []

        class FakePage:
            keyboard = type("Keyboard", (), {"press": lambda _self, key: calls.append(("press", key))})()

            def evaluate(self, script, arg=None):
                calls.append(("evaluate", script, arg))
                if script == xunfei.JS.GET_EDITOR_TEXT:
                    return ""
                return True

            def wait_for_timeout(self, milliseconds):
                calls.append(("wait", milliseconds))

        session._cleanup_after_item(FakePage())

        self.assertTrue(any(call[0] == "evaluate" and call[1] == xunfei.JS.CLOSE_ALL_MODALS for call in calls))
        self.assertTrue(any(call[0] == "evaluate" and call[1] == xunfei.JS.CLEAR_EDITOR for call in calls))
        self.assertFalse(any(call[0] == "goto" for call in calls))

    def test_concurrent_first_session_creation_reuses_one_sync_session(self):
        original_session = xunfei_runtime._session
        original_available = xunfei_runtime.is_available
        original_health = xunfei_runtime._session_is_healthy
        original_session_class = xunfei_runtime.XunFeiSession
        created = []

        class FakeSession:
            _logged_in = True
            _page = object()
            _ctx = object()

            def __init__(self, voice_key="amanda"):
                self.voice_key = voice_key
                created.append(self)

            def login(self, login_timeout=300):
                return None

        xunfei_runtime._session = None
        xunfei_runtime.is_available = lambda: True
        xunfei_runtime._session_is_healthy = lambda session: session is not None
        xunfei_runtime.XunFeiSession = FakeSession
        try:
            async def create_both():
                return await asyncio.gather(
                    xunfei.ensure_session("amanda"),
                    xunfei.ensure_session("george"),
                )

            first, second = asyncio.run(create_both())
        finally:
            xunfei_runtime._session = original_session
            xunfei_runtime.is_available = original_available
            xunfei_runtime._session_is_healthy = original_health
            xunfei_runtime.XunFeiSession = original_session_class

        self.assertIs(first, second)
        self.assertEqual(len(created), 1)

    def test_failed_login_keeps_candidate_session_for_disconnect_classification(self):
        original_session = xunfei_runtime._session
        original_available = xunfei_runtime.is_available
        original_session_class = xunfei_runtime.XunFeiSession

        class FailedLoginSession:
            def __init__(self, voice_key="amanda"):
                self.voice_key = voice_key
                self._browser_disconnected = False

            def login(self, **_kwargs):
                raise RuntimeError("Target page, context or browser has been closed")

            def close(self):
                self._browser_disconnected = True

        xunfei_runtime._session = None
        xunfei_runtime.is_available = lambda: True
        xunfei_runtime.XunFeiSession = FailedLoginSession
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(xunfei.ensure_session())
            self.assertIsInstance(xunfei_runtime._session, FailedLoginSession)
            self.assertTrue(xunfei_runtime._session._browser_disconnected)
        finally:
            xunfei_runtime._session = original_session
            xunfei_runtime.is_available = original_available
            xunfei_runtime.XunFeiSession = original_session_class


if __name__ == "__main__":
    unittest.main()
