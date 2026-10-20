"""好友申请智能审批插件测试。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from maibot_sdk import PluginPaths

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "maibot_friend_gate_test"


def load_plugin_package() -> None:
    """把插件目录作为包加载，便于相对导入。"""

    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


load_plugin_package()
plugin_module = importlib.import_module(f"{PACKAGE_NAME}.plugin")
config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
decision_module = importlib.import_module(f"{PACKAGE_NAME}.decision")
napcat_module = importlib.import_module(f"{PACKAGE_NAME}.napcat")


class FakeNapCatServer:
    """模拟 NapCat 正向 WebSocket 服务。"""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/", self._handle_ws)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.port = 0
        self.ws: web.WebSocketResponse | None = None
        self.connected = asyncio.Event()
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.stranger_info: dict[str, Any] = {
            "uin": "10001",
            "nick": "测试申请人",
            "sex": "unknown",
            "age": 0,
            "qqLevel": 12,
            "longNick": "测试签名",
            "country": "",
            "province": "",
            "city": "",
            "regTime": 1600000000,
            "is_vip": False,
        }
        self.friend_list: list[dict[str, Any]] = [{"user_id": 3864117714, "nickname": "麦麦"}]
        self.send_fail = False

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                action = str(payload.get("action") or "")
                params = payload.get("params") or {}
                echo = payload.get("echo")
                self.actions.append((action, params))
                if action == "send_private_msg" and self.send_fail:
                    await ws.send_json(
                        {
                            "status": "failed",
                            "retcode": 100,
                            "data": None,
                            "message": "send failed",
                            "echo": echo,
                        }
                    )
                    continue
                data: Any = None
                if action == "get_login_info":
                    data = {"user_id": 3864117714, "nickname": "麦麦"}
                elif action == "get_stranger_info":
                    data = dict(self.stranger_info)
                    data["uin"] = str(params.get("user_id") or "10001")
                elif action == "get_friend_list":
                    data = list(self.friend_list)
                await ws.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "message": "",
                        "echo": echo,
                    }
                )
        finally:
            self.ws = None
        return ws

    async def push_friend_request(self, user_id: int = 10001, flag: str = "req-1", comment: str = "你好") -> None:
        assert self.ws is not None
        await self.ws.send_json(
            {
                "post_type": "request",
                "request_type": "friend",
                "user_id": user_id,
                "comment": comment,
                "flag": flag,
            }
        )


class FakeContext:
    """最小的 PluginContext 替身。"""

    def __init__(self, data_dir: Path) -> None:
        self.paths = PluginPaths(data_dir=data_dir, runtime_dir=data_dir)
        self.logger = logging.getLogger("test.friend_gate")
        self.logger.setLevel(logging.DEBUG)
        self.bot_config = {"nickname": "麦麦"}
        self.personality_config = {"personality": "一个友好的 QQ 机器人"}
        self.llm_response = {
            "success": True,
            "response": '{"approve": true, "confidence": 0.9, "reason": "来意正常", "remark": "测试"}',
        }
        self.llm_calls: list[Any] = []

        class LLM:
            def __init__(self, outer: "FakeContext") -> None:
                self.outer = outer

            async def generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
                self.outer.llm_calls.append((prompt, kwargs))
                return dict(self.outer.llm_response)

        class Config:
            def __init__(self, outer: "FakeContext") -> None:
                self.outer = outer

            async def get(self, key: str, default: Any = None) -> Any:
                if key == "bot":
                    return dict(self.outer.bot_config)
                if key == "personality":
                    return dict(self.outer.personality_config)
                return default

        self.llm = LLM(self)
        self.config = Config(self)


class FriendGateTestCase(unittest.IsolatedAsyncioTestCase):
    """端到端测试。"""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.server = FakeNapCatServer()
        await self.server.start()
        self.context = FakeContext(self.data_dir)
        self.plugin = plugin_module.FriendGatePlugin()
        self.plugin._set_context(self.context)
        self.config_data = self.plugin.build_default_config()
        self.config_data["connection"]["ws_url"] = f"ws://127.0.0.1:{self.server.port}"
        self.config_data["connection"]["access_token"] = ""
        self.config_data["connection"]["auto_discover"] = False
        self.config_data["connection"]["action_timeout_seconds"] = 5
        self.config_data["notify"]["admin_qqs"] = ["99999"]
        self.config_data["notify"]["include_avatar"] = False
        self.config_data["strategy"]["welcome_delay_seconds"] = 0
        self.plugin.set_plugin_config(self.config_data)

    async def asyncTearDown(self) -> None:
        await self.plugin.on_unload()
        await self.server.stop()
        self.temp_dir.cleanup()

    async def _start(self) -> None:
        await self.plugin.on_load()
        await asyncio.wait_for(self.server.connected.wait(), timeout=5)
        # 等 login_info 返回。
        await asyncio.sleep(0.1)

    async def _wait_processed(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.plugin._processed:
                return
            await asyncio.sleep(0.05)
        self.fail("等待处理结果超时")

    async def _wait_for_text(self, needle: str, timeout: float = 5.0) -> str:
        """等待某条私聊文本出现。"""

        deadline = time.time() + timeout
        while time.time() < deadline:
            for action, params in list(self.server.actions):
                if action != "send_private_msg":
                    continue
                for segment in params.get("message", []):
                    text = str(segment.get("data", {}).get("text", ""))
                    if needle in text:
                        return text
            await asyncio.sleep(0.05)
        self.fail(f"等待私聊文本超时: {needle}")
        return ""

    async def test_approve_flow(self) -> None:
        await self._start()
        await self.server.push_friend_request(comment="麦麦你好，我是群友")
        await self._wait_processed()
        await self._wait_for_text("已通过好友申请")

        actions = self.server.actions
        set_actions = [params for action, params in actions if action == "set_friend_add_request"]
        self.assertEqual(len(set_actions), 1)
        self.assertEqual(set_actions[0]["flag"], "req-1")
        self.assertTrue(set_actions[0]["approve"])
        self.assertEqual(set_actions[0].get("remark"), "测试")

        sends = [params for action, params in actions if action == "send_private_msg"]
        self.assertGreaterEqual(len(sends), 1)
        # 欢迎语 + 管理员通知
        texts = [segment.get("data", {}).get("text", "") for params in sends for segment in params.get("message", [])]
        self.assertTrue(any("很高兴认识" in text for text in texts))
        self.assertTrue(any("已通过好友申请" in text for text in texts))

        processed = list(self.plugin._processed.values())
        self.assertEqual(processed[0]["status"], "approved")
        self.assertTrue((self.data_dir / "state.json").is_file())

    async def test_reject_flow(self) -> None:
        self.context.llm_response = {
            "success": True,
            "response": '{"approve": false, "confidence": 0.95, "reason": "广告"}',
        }
        await self._start()
        await self.server.push_friend_request(comment="加群推广")
        await self._wait_processed()

        set_actions = [params for action, params in self.server.actions if action == "set_friend_add_request"]
        self.assertEqual(len(set_actions), 1)
        self.assertFalse(set_actions[0]["approve"])

        processed = list(self.plugin._processed.values())
        self.assertEqual(processed[0]["status"], "rejected")

    async def test_blacklist_keyword_skips_llm(self) -> None:
        self.config_data["filters"]["blacklist_keywords"] = ["推广"]
        self.plugin.set_plugin_config(self.config_data)
        await self._start()
        await self.server.push_friend_request(comment="加群推广")
        await self._wait_processed()

        self.assertEqual(self.context.llm_calls, [])
        processed = list(self.plugin._processed.values())
        self.assertEqual(processed[0]["status"], "rejected")

    async def test_llm_failure_manual(self) -> None:
        self.context.llm_response = {"success": False, "error": "model down"}
        await self._start()
        await self.server.push_friend_request(comment="你好")
        deadline = time.time() + 5
        while time.time() < deadline and not self.plugin._pending:
            await asyncio.sleep(0.05)
        # 等待状态变为 manual
        while time.time() < deadline:
            item = self.plugin._pending.get("10001")
            if item and item.get("status") == "manual":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.plugin._pending["10001"]["status"], "manual")
        set_actions = [params for action, params in self.server.actions if action == "set_friend_add_request"]
        self.assertEqual(set_actions, [])

    async def test_duplicate_event_only_once(self) -> None:
        await self._start()
        await self.server.push_friend_request(flag="dup-1")
        await self._wait_processed()
        await self.server.push_friend_request(flag="dup-1")
        await asyncio.sleep(0.5)
        set_actions = [params for action, params in self.server.actions if action == "set_friend_add_request"]
        self.assertEqual(len(set_actions), 1)

    async def test_manual_command_approve(self) -> None:
        self.config_data["strategy"]["mode"] = "manual"
        self.plugin.set_plugin_config(self.config_data)
        await self._start()
        await self.server.push_friend_request(comment="你好")
        deadline = time.time() + 5
        while time.time() < deadline and not self.plugin._pending:
            await asyncio.sleep(0.05)
        self.assertIn("10001", self.plugin._pending)

        success, message, intercept = await self.plugin.handle_approve_command(
            user_id="99999",
            group_id="",
            is_local_operator=False,
            matched_groups={"target_qq": "10001", "remark": "朋友"},
        )
        self.assertTrue(success)
        self.assertTrue(intercept)
        self.assertIn("已同意", message or "")

        set_actions = [params for action, params in self.server.actions if action == "set_friend_add_request"]
        self.assertEqual(len(set_actions), 1)
        self.assertTrue(set_actions[0]["approve"])
        self.assertEqual(set_actions[0]["remark"], "朋友")

    async def test_non_admin_command_ignored(self) -> None:
        success, message, intercept = await self.plugin.handle_status_command(
            user_id="12345",
            group_id="",
            is_local_operator=False,
        )
        self.assertFalse(success)
        self.assertIsNone(message)
        self.assertFalse(intercept)

    async def test_state_reload(self) -> None:
        await self._start()
        await self.server.push_friend_request(flag="reload-1")
        await self._wait_processed()
        await self.plugin.on_unload()

        second = plugin_module.FriendGatePlugin()
        second._set_context(self.context)
        second.set_plugin_config(self.config_data)
        await second.on_load()
        self.assertIn("reload-1", second._processed)
        await second.on_unload()


class ParserTestCase(unittest.TestCase):
    """模型输出解析测试。"""

    def test_parse_json_fenced(self) -> None:
        decision = decision_module.parse_review_decision(
            '```json\n{"approve": true, "confidence": 80, "reason": "ok"}\n```'
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertTrue(decision.approve)
        self.assertAlmostEqual(decision.confidence, 0.8)

    def test_parse_text_with_reasoning(self) -> None:
        decision = decision_module.parse_review_decision(
            '先看一下：{"approve": false, "confidence": 0.9, "reason": "广告"} 完毕'
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertFalse(decision.approve)

    def test_parse_invalid(self) -> None:
        self.assertIsNone(decision_module.parse_review_decision("我无法判断"))
        self.assertIsNone(decision_module.parse_review_decision(""))

    def test_remark_sanitize(self) -> None:
        self.assertEqual(decision_module.sanitize_remark("  你好\n世界  "), "你好 世界")
        self.assertEqual(decision_module.sanitize_remark("", fallback="昵称"), "昵称")


class DiscoveryTestCase(unittest.TestCase):
    """自动发现测试。"""

    def test_discover_napcat_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = root / "plugins" / "Napcat-Adapter"
            adapter.mkdir(parents=True)
            (adapter / "config.toml").write_text(
                '[napcat_server]\nhost = "127.0.0.1"\nport = 7998\ntoken = "secret"\n',
                encoding="utf-8",
            )
            endpoint = napcat_module.discover_endpoint(root)
            self.assertIsNotNone(endpoint)
            assert endpoint is not None
            self.assertEqual(endpoint.ws_url, "ws://127.0.0.1:7998")
            self.assertEqual(endpoint.access_token, "secret")

    def test_discover_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(napcat_module.discover_endpoint(Path(temp)))


if __name__ == "__main__":
    unittest.main()
