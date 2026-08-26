from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import xunfei_peiyin as xunfei
from xunfei_peiyin import XunFeiSession


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

    def test_composite_payload_ignores_empty_leading_segment_for_top_level_voice(self):
        session = XunFeiSession()
        voice_catalog = {
            "speaker:one": {"name": "One", "speaker_no": 1001},
            "speaker:two": {"name": "Two", "speaker_no": 1002},
        }
        work = {
            "item_ids": ["q1"],
            "item_count": 1,
            "items": [{
                "segments": [
                    {"voice_key": "speaker:one", "text": ""},
                    {"voice_key": "speaker:two", "text": "actual"},
                ],
            }],
        }

        with mock.patch.dict(xunfei.VOICES, voice_catalog, clear=False):
            payload = session._build_composite_payload(work)

        self.assertEqual(payload["speakerNo"], 1002)
        self.assertEqual([item["speakerNo"] for item in payload["synthInfos"]], [1002])

    def test_composite_submit_accepts_numeric_success_code_and_marks_login_expired(self):
        session = XunFeiSession()
        with mock.patch.object(
            xunfei,
            "_safe_eval",
            return_value={"httpStatus": 200, "data": {"retCode": 0, "tempWorksId": 123}},
        ):
            self.assertEqual(session._post_multiple_speaker_work(object(), {}), "123")

        session._logged_in = True
        with mock.patch.object(
            xunfei,
            "_safe_eval",
            return_value={"httpStatus": 200, "data": {"retCode": "999999", "retMsg": "用户未登录"}},
        ), self.assertRaises(xunfei.XunfeiLoginRequired):
            session._post_multiple_speaker_work(object(), {})
        self.assertFalse(session._logged_in)

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

    def test_composite_order_uses_page_source_and_clears_temporary_capture(self):
        session = XunFeiSession()
        session._logged_in = True
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        session._page = page
        work = {
            "work_id": "composite:order",
            "works_name": "order-test",
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

        with mock.patch.object(
            session,
            "_mark_works_cutoff",
        ) as mark_cutoff, mock.patch.object(
            session,
            "_post_multiple_speaker_work",
            return_value="temporary-id",
        ), mock.patch.object(
            session,
            "_signed_api_post",
            return_value={"data": {"payOrder": {"worksId": "final-id"}}},
        ) as signed_post, mock.patch.object(
            xunfei,
            "_safe_eval",
            return_value={"fromSpread": "affiliate"},
        ):
            pending = session._generate_pending_composite(work)

        self.assertEqual(pending["works_id"], "final-id")
        self.assertEqual(mark_cutoff.call_count, 2)
        self.assertEqual(
            signed_post.call_args.args[2]["fromSpread"],
            "affiliate",
        )

    def test_composite_payload_contains_multi_speaker_segments_and_editor_break(self):
        session = XunFeiSession()
        voice_catalog = {
            "speaker:one": {
                "name": "One",
                "speaker_no": 1001,
                "common_id": 2001,
                "language": ["普通话"],
                "img_url": "https://example.test/one.png",
            },
            "speaker:two": {
                "name": "Two",
                "speaker_no": 1002,
                "common_id": 2002,
                "language": ["普通话"],
                "img_url": "https://example.test/two.png",
            },
        }
        work = {
            "work_id": "composite:test",
            "boundary_ms": 2000,
            "works_name": "payload-test",
            "item_ids": ["q1", "q2"],
            "item_count": 2,
            "items": [
                {
                    "item_id": "q1",
                    "segments": [{
                        "voice_key": "speaker:one",
                        "speed": 48,
                        "pitch": 52,
                        "volume": 55,
                        "text": "第一段",
                    }],
                },
                {
                    "item_id": "q2",
                    "segments": [{
                        "voice_key": "speaker:two",
                        "speed": 50,
                        "pitch": 50,
                        "volume": 50,
                        "text": "第二段",
                    }],
                },
            ],
        }

        with mock.patch.dict(xunfei.VOICES, voice_catalog, clear=False):
            payload = session._build_composite_payload(work)

        self.assertEqual([item["speakerNo"] for item in payload["synthInfos"]], [1001, 1002])
        self.assertEqual([item["speakingText"] for item in payload["synthInfos"]], ["第一段", "第二段"])
        editor_doc = json.loads(payload["editText"])
        self.assertEqual(editor_doc["content"][0]["content"][1]["type"], "break")
        self.assertEqual(editor_doc["content"][0]["content"][1]["attrs"]["value"], 2000)
        self.assertEqual(payload["speakingRate"], 48)
        self.assertEqual(payload["speakingVolumn"], 2)

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
        original_session = xunfei._session
        original_available = xunfei.is_available
        original_health = xunfei._session_is_healthy
        seen_threads = []
        fake_session = _HealthyFakeSession()
        xunfei._session = fake_session
        xunfei.is_available = lambda: True

        def health_check(session):
            seen_threads.append(threading.current_thread())
            return session is fake_session

        xunfei._session_is_healthy = health_check
        try:
            result = asyncio.run(xunfei.ensure_session())
        finally:
            xunfei._session = original_session
            xunfei.is_available = original_available
            xunfei._session_is_healthy = original_health

        self.assertIs(result, fake_session)
        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(seen_threads[0], threading.current_thread())

    def test_all_playwright_sync_calls_share_one_dedicated_thread(self):
        seen_threads = []

        def record_thread():
            seen_threads.append(threading.current_thread())

        async def run_calls():
            await xunfei._run_playwright_sync(record_thread)
            await xunfei._run_playwright_sync(record_thread)

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
                xunfei.urllib.request,
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
        original_session = xunfei._session
        original_available = xunfei.is_available
        original_health = xunfei._session_is_healthy
        original_session_class = xunfei.XunFeiSession
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

        xunfei._session = None
        xunfei.is_available = lambda: True
        xunfei._session_is_healthy = lambda session: session is not None
        xunfei.XunFeiSession = FakeSession
        try:
            async def create_both():
                return await asyncio.gather(
                    xunfei.ensure_session("amanda"),
                    xunfei.ensure_session("george"),
                )

            first, second = asyncio.run(create_both())
        finally:
            xunfei._session = original_session
            xunfei.is_available = original_available
            xunfei._session_is_healthy = original_health
            xunfei.XunFeiSession = original_session_class

        self.assertIs(first, second)
        self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
