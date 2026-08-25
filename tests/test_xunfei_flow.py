from __future__ import annotations

import asyncio
import threading
import unittest

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
    def test_text_input_is_a_single_paste_like_insert(self):
        page = _FakePage()
        text = "Reporter: " + ("This is a long sentence. " * 40)

        self.assertTrue(XunFeiSession._type_text(page, text))
        self.assertEqual(page.keyboard.inserted, [text])
        self.assertEqual(page.keyboard.typed, [])

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

    def test_ai_switch_logic_requires_an_off_state(self):
        self.assertIn("return 'already_off'", xunfei.JS.CLICK_AI_SWITCH)
        self.assertIn("return 'clicked'", xunfei.JS.CLICK_AI_SWITCH)
        self.assertIn("CHECK_AI_SWITCH_OFF", dir(xunfei.JS))

        session = XunFeiSession()

        class FakePage:
            def evaluate(self, script, arg=None):
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
            self.assertEqual(page.evaluate(xunfei.JS.GET_AI_SWITCH_STATE), "on")
            self.assertEqual(page.evaluate(xunfei.JS.CLICK_AI_SWITCH), "clicked")
            page.wait_for_timeout(30)
            self.assertEqual(page.evaluate(xunfei.JS.GET_AI_SWITCH_STATE), "off")
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
