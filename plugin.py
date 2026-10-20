"""好友申请智能审批插件。

插件直接连接 NapCat 的正向 WebSocket，接收 ``request.friend`` 事件，
读取申请人的公开资料与验证消息，交给模型判断是否通过；模型不可用或置信度
不足时按配置转人工、拒绝或通过。通过后可以自动设置备注并发送欢迎语。

安全提示：验证消息属于不可信输入，插件会在提示词中明确要求模型忽略其中的
“指令”，并且默认不把手机号、邮箱等隐私字段交给模型。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import Command, MaiBotPlugin

from .config import FriendGateConfig, normalize_qq, normalize_qq_list
from .decision import (
    ApplicantProfile,
    FriendRequestRecord,
    ReviewDecision,
    build_review_messages,
    contains_blacklisted_keyword,
    parse_review_decision,
    sanitize_remark,
    sex_text,
    summarize_recent,
)
from .napcat import NapCatClient, NapCatEndpoint, NapCatError, discover_endpoint, ensure_action_ok

_STATE_VERSION = 1
_PROCESSED_RETENTION_SECONDS = 30 * 24 * 3600
_DECISION_TIME_RETENTION_SECONDS = 24 * 3600
_FRIEND_CACHE_SECONDS = 300


class FriendGatePlugin(MaiBotPlugin):
    """好友申请智能审批插件。"""

    config_model = FriendGateConfig

    def __init__(self) -> None:
        super().__init__()
        self._state_path: Path | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._processed: dict[str, dict[str, Any]] = {}
        self._processed_flags: set[str] = set()
        self._decision_times: list[float] = []
        self._stats: dict[str, int] = {
            "approved": 0,
            "rejected": 0,
            "manual": 0,
            "error": 0,
            "skipped": 0,
        }
        self._friend_cache: dict[str, tuple[bool, float]] = {}
        self._client: NapCatClient | None = None
        self._endpoint: NapCatEndpoint | None = None
        self._started = False
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        """加载状态并启动 NapCat 连接。"""

        self._ensure_state_path()
        self._load_state()
        self._endpoint = self._resolve_endpoint()
        if not self.config.plugin.enabled:
            self.ctx.logger.info("好友申请智能审批已加载，但插件当前未启用")
            return
        if self._endpoint is None:
            self.ctx.logger.warning(
                "好友申请智能审批未找到 NapCat 连接信息：请在插件配置中填写 ws_url/access_token，"
                "或确保 Napcat 适配器已安装并生成 config.toml"
            )
            return
        self._start_client()
        self.ctx.logger.info("好友申请智能审批已加载，正在连接 NapCat: %s", self._endpoint.safe_url)

    async def on_unload(self) -> None:
        """停止连接并保存状态。"""

        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self._stop_client()
        self._save_state()
        self.ctx.logger.info("好友申请智能审批已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热更新。"""

        del config_data
        if scope != "self":
            return
        self._save_state()

        old_endpoint = self._endpoint
        new_endpoint = self._resolve_endpoint()
        self._endpoint = new_endpoint

        if not self.config.plugin.enabled:
            await self._stop_client()
            self.ctx.logger.info("好友申请智能审批配置已更新：插件已停用（version=%s）", version)
            return

        if new_endpoint is None:
            await self._stop_client()
            self.ctx.logger.warning("好友申请智能审批配置已更新，但未找到有效的 NapCat 连接信息")
            return

        if old_endpoint != new_endpoint or self._client is None:
            await self._stop_client()
            self._start_client()
        self.ctx.logger.info("好友申请智能审批配置已更新（version=%s）", version)

    # ---------------------------------------------------------------- NapCat 客户端

    def _ensure_state_path(self) -> None:
        """初始化状态文件路径。"""

        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "state.json"

    def _resolve_endpoint(self) -> NapCatEndpoint | None:
        """解析配置或自动发现 NapCat 连接。"""

        ws_url = str(self.config.connection.ws_url or "").strip()
        token = str(self.config.connection.access_token or "").strip()
        if ws_url:
            if not ws_url.startswith(("ws://", "wss://")):
                ws_url = f"ws://{ws_url}"
            return NapCatEndpoint(ws_url=ws_url, access_token=token)

        if not self.config.connection.auto_discover:
            return None

        try:
            mai_root = Path(__file__).resolve().parents[2]
        except IndexError:
            return None
        return discover_endpoint(mai_root)

    def _start_client(self) -> None:
        """创建并启动客户端。"""

        if self._client is not None or self._endpoint is None:
            return
        self._client = NapCatClient(
            self._endpoint,
            logger=self.ctx.logger,
            action_timeout=self.config.connection.action_timeout_seconds,
            reconnect_initial=self.config.connection.reconnect_initial_seconds,
            reconnect_max=self.config.connection.reconnect_max_seconds,
            on_event=self._on_napcat_event,
            on_connected=self._on_napcat_connected,
        )
        self._client.start()
        self._started = True

    async def _stop_client(self) -> None:
        """停止客户端。"""

        client = self._client
        self._client = None
        self._started = False
        if client is not None:
            await client.stop()

    async def _on_napcat_connected(self, client: NapCatClient) -> None:
        """连接建立后的回调。"""

        self._friend_cache.clear()
        self.ctx.logger.info("好友申请审批已连上 NapCat，账号=%s", client.self_id or "未知")

    def _on_napcat_event(self, payload: dict[str, Any]) -> None:
        """接收 NapCat 事件（同步入口，异步处理）。"""

        if not self.config.plugin.enabled:
            return
        post_type = str(payload.get("post_type") or "").strip()
        request_type = str(payload.get("request_type") or "").strip()
        if post_type != "request" or request_type != "friend":
            return

        user_id = normalize_qq(payload.get("user_id"))
        flag = str(payload.get("flag") or "").strip()
        comment = str(payload.get("comment") or payload.get("message") or payload.get("msg") or "").strip()
        if not user_id:
            self.ctx.logger.warning("收到缺少 user_id 的好友申请事件，已忽略")
            return

        task = asyncio.create_task(self._handle_friend_request(user_id, flag, comment))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """清理已完成的后台任务并记录异常。"""

        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.ctx.logger.warning("好友申请处理任务异常: %s", exc, exc_info=exc)

    # ---------------------------------------------------------------- 申请处理

    async def _handle_friend_request(self, user_id: str, flag: str, comment: str) -> None:
        """处理一条好友申请。"""

        if self._is_duplicate(user_id, flag):
            self.ctx.logger.debug("忽略重复好友申请: qq=%s flag=%s", user_id, flag)
            return

        if not self._within_rate_limit():
            record = FriendRequestRecord(
                flag=flag,
                user_id=user_id,
                comment=comment,
                source="rate_limited",
            )
            record.status = "manual"
            record.decision = ReviewDecision(
                approve=False,
                confidence=0.0,
                reason="触发自动处理频率上限",
                manual=True,
                source="rate_limit",
            ).to_dict()
            self._pending[user_id] = record.to_dict()
            self._save_state()
            await self._notify_manual(record)
            return

        record = FriendRequestRecord(flag=flag, user_id=user_id, comment=comment)
        self._pending[user_id] = record.to_dict()
        self._save_state()

        try:
            record.profile = await self._fetch_profile(user_id)
            self._pending[user_id] = record.to_dict()
            self._save_state()

            decision = await self._decide(record)
            await self._apply(record, decision)
        except Exception as exc:
            self.ctx.logger.error("处理好友申请失败: qq=%s error=%s", user_id, exc, exc_info=True)
            record.status = "error"
            record.last_error = str(exc)
            self._pending[user_id] = record.to_dict()
            self._save_state()
            await self._notify_error(record, str(exc))

    def _is_duplicate(self, user_id: str, flag: str) -> bool:
        """判断申请是否已经处理或正在处理。"""

        if flag and (flag in self._processed_flags or flag in self._processed):
            return True
        if user_id in self._pending:
            return True
        if not flag:
            # 没有 flag 的申请无法执行动作，只按 QQ + 最近时间做一次去重。
            now = time.time()
            for item in self._processed.values():
                if item.get("user_id") == user_id and now - float(item.get("processed_at") or 0) < 3600:
                    return True
        return False

    def _within_rate_limit(self) -> bool:
        """检查小时 / 每日处理上限。"""

        now = time.time()
        self._decision_times = [value for value in self._decision_times if now - value < _DECISION_TIME_RETENTION_SECONDS]
        hour_count = sum(1 for value in self._decision_times if now - value < 3600)
        today_count = self._count_today()

        if hour_count >= max(1, int(self.config.strategy.max_decisions_per_hour)):
            return False
        if today_count >= max(1, int(self.config.strategy.max_decisions_per_day)):
            return False
        return True

    def _register_decision(self) -> None:
        """记录一次自动处理时间。"""

        now = time.time()
        self._decision_times.append(now)
        self._decision_times = [value for value in self._decision_times if now - value < _DECISION_TIME_RETENTION_SECONDS]

    def _local_now(self) -> datetime:
        """返回配置时区的当前时间。"""

        timezone = str(self.config.plugin.timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
        try:
            return datetime.now(tz=ZoneInfo(timezone))
        except (ZoneInfoNotFoundError, ValueError):
            return datetime.now()

    def _count_today(self) -> int:
        """统计今天已经自动处理的申请数。"""

        today = self._local_now().date()
        count = 0
        for value in self._decision_times:
            try:
                if datetime.fromtimestamp(value, tz=self._local_now().tzinfo).date() == today:
                    count += 1
            except (OverflowError, OSError, ValueError):
                continue
        return count

    async def _fetch_profile(self, user_id: str) -> ApplicantProfile:
        """读取申请人公开资料和好友状态。"""

        profile = ApplicantProfile(user_id=user_id)
        client = self._client
        if client is None or not client.connected:
            return profile

        try:
            payload: dict[str, Any] = {"user_id": int(user_id) if user_id.isdigit() else user_id}
            payload["no_cache"] = True
            data = await client.call_data("get_stranger_info", payload)
            profile = ApplicantProfile.from_stranger_info(user_id, data)
        except Exception as exc:
            self.ctx.logger.debug("读取申请人资料失败: qq=%s error=%s", user_id, exc)

        try:
            profile.already_friend = await self._is_friend(user_id)
        except Exception as exc:
            self.ctx.logger.debug("检查好友状态失败: qq=%s error=%s", user_id, exc)
        return profile

    async def _is_friend(self, user_id: str) -> bool:
        """检查申请人是否已经是好友（带短期缓存）。"""

        now = time.time()
        cached = self._friend_cache.get(user_id)
        if cached is not None and now - cached[1] < _FRIEND_CACHE_SECONDS:
            return cached[0]

        client = self._client
        if client is None or not client.connected:
            return False

        data = await client.call_data("get_friend_list", {"no_cache": False})
        friend_ids: set[str] = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    qq = normalize_qq(item.get("user_id"))
                    if qq:
                        friend_ids.add(qq)
        elif isinstance(data, dict):
            for item in data.get("data") or []:
                if isinstance(item, dict):
                    qq = normalize_qq(item.get("user_id"))
                    if qq:
                        friend_ids.add(qq)

        is_friend = user_id in friend_ids
        self._friend_cache[user_id] = (is_friend, now)
        return is_friend

    async def _decide(self, record: FriendRequestRecord) -> ReviewDecision:
        """根据过滤规则和模型输出生成审核结论。"""

        strategy = self.config.strategy
        user_id = record.user_id
        comment = record.comment.strip()

        whitelist = set(normalize_qq_list(self.config.filters.whitelist_qqs))
        blacklist = set(normalize_qq_list(self.config.filters.blacklist_qqs))

        if user_id in whitelist:
            return ReviewDecision(True, 1.0, "命中白名单", source="filter")
        if user_id in blacklist:
            return ReviewDecision(False, 1.0, "命中黑名单", source="filter")

        profile = record.profile or ApplicantProfile(user_id=user_id)
        if profile.already_friend and self.config.filters.skip_if_already_friend:
            return ReviewDecision(True, 1.0, "申请人已经是好友", noop=True, source="skip")

        keyword = contains_blacklisted_keyword(comment, list(self.config.filters.blacklist_keywords))
        if keyword:
            return ReviewDecision(False, 1.0, f"验证消息命中黑名单关键词：{keyword}", source="filter")

        if self.config.filters.reject_empty_comment and not comment:
            return ReviewDecision(False, 1.0, "验证消息为空", source="filter")

        if (
            self.config.filters.min_qq_level > 0
            and profile.level > 0
            and profile.level < self.config.filters.min_qq_level
        ):
            return ReviewDecision(
                False,
                1.0,
                f"QQ 等级 {profile.level} 低于配置下限 {self.config.filters.min_qq_level}",
                source="filter",
            )

        mode = str(strategy.mode or "llm").strip().lower()
        if mode == "auto_approve":
            return ReviewDecision(True, 1.0, "无条件自动通过模式", source="mode")
        if mode == "manual":
            return ReviewDecision(False, 0.0, "手动审批模式", manual=True, source="mode")

        return await self._decide_with_llm(record)

    async def _decide_with_llm(self, record: FriendRequestRecord) -> ReviewDecision:
        """调用模型判断申请。"""

        strategy = self.config.strategy
        bot_name, persona = await self._get_persona_and_name()
        messages = build_review_messages(
            record,
            persona=persona,
            bot_name=bot_name,
            extra_criteria=self.config.prompt.extra_criteria,
            recent_summary=summarize_recent(self._processed),
        )

        try:
            result = await self.ctx.llm.generate(
                messages,
                task_name=str(strategy.task_name or "utils"),
                model_name=str(strategy.model_name or ""),
                temperature=float(strategy.temperature),
                max_tokens=int(strategy.max_tokens),
            )
        except Exception as exc:
            self.ctx.logger.warning("好友申请审核模型调用异常: %s", exc)
            return self._fallback_decision(f"模型调用异常：{exc}")

        if not isinstance(result, dict) or not bool(result.get("success")):
            error = ""
            if isinstance(result, dict):
                error = str(result.get("error") or "")
            return self._fallback_decision(f"模型调用失败：{error or '未知错误'}")

        raw = str(result.get("response") or "").strip()
        decision = parse_review_decision(raw)
        if decision is None:
            return self._fallback_decision("模型输出无法解析为 JSON 结论")

        if decision.confidence < float(strategy.min_confidence):
            return self._fallback_decision(
                f"模型置信度 {decision.confidence:.2f} 低于阈值 {strategy.min_confidence:.2f}"
            )

        if decision.approve and not decision.remark and self.config.strategy.auto_remark:
            decision.remark = sanitize_remark(
                record.profile.nickname if record.profile else "",
                fallback="",
            )
        return decision

    def _fallback_decision(self, reason: str) -> ReviewDecision:
        """按配置生成模型失败 / 不确定时的兜底结论。"""

        action = str(self.config.strategy.uncertain_action or "manual").strip().lower()
        if action == "approve":
            return ReviewDecision(True, 0.0, f"{reason}；按配置通过", source="fallback")
        if action == "reject":
            return ReviewDecision(False, 0.0, f"{reason}；按配置拒绝", source="fallback")
        return ReviewDecision(False, 0.0, reason, manual=True, source="fallback")

    async def _get_persona_and_name(self) -> tuple[str, str]:
        """读取主程序中的机器人昵称与人设。"""

        bot_name = "麦麦"
        persona = str(self.config.prompt.persona_override or "").strip()

        try:
            bot_config = await self.ctx.config.get("bot", {})
            if isinstance(bot_config, dict):
                bot_name = str(bot_config.get("nickname") or bot_name).strip() or bot_name
        except Exception:
            pass

        if not persona:
            try:
                personality_config = await self.ctx.config.get("personality", {})
                if isinstance(personality_config, dict):
                    persona = str(personality_config.get("personality") or "").strip()
            except Exception:
                pass
        return bot_name, persona

    async def _apply(
        self,
        record: FriendRequestRecord,
        decision: ReviewDecision,
        *,
        notify: bool = True,
    ) -> tuple[bool, str]:
        """执行审核结论。

        Returns:
            tuple[bool, str]: ``(是否完成动作, 面向命令调用者的提示文本)``。
        """

        if decision.noop:
            self._finish_record(record, "skipped", decision)
            return True, f"QQ {record.user_id} 已经是好友，已跳过。"

        if decision.manual:
            record.status = "manual"
            record.decision = decision.to_dict()
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            if notify:
                await self._notify_manual(record)
            return False, f"QQ {record.user_id} 已转人工处理。"

        if self.config.plugin.dry_run:
            dry_decision = ReviewDecision(
                approve=decision.approve,
                confidence=decision.confidence,
                reason=f"[演练] {decision.reason}",
                remark=decision.remark,
                manual=True,
                source="dry_run",
            )
            record.status = "manual"
            record.decision = dry_decision.to_dict()
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            if notify:
                await self._notify_manual(record, dry_run=True)
            return False, f"[演练] QQ {record.user_id} 的判断结果是{'通过' if decision.approve else '拒绝'}。"

        if not record.flag:
            record.status = "manual"
            record.decision = ReviewDecision(
                approve=False,
                confidence=0.0,
                reason="申请事件缺少 flag，无法自动处理",
                manual=True,
                source="missing_flag",
            ).to_dict()
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            if notify:
                await self._notify_manual(record)
            return False, f"QQ {record.user_id} 的申请缺少 flag，已转人工。"

        client = self._client
        if client is None or not client.connected:
            record.status = "error"
            record.last_error = "NapCat 未连接"
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            if notify:
                await self._notify_error(record, record.last_error)
            return False, "NapCat 未连接，无法处理好友申请。"

        remark = ""
        if decision.approve and self.config.strategy.auto_remark:
            template = str(self.config.strategy.remark_template or "{nickname}")
            fallback = record.profile.nickname if record.profile else ""
            try:
                rendered = template.format(
                    nickname=fallback,
                    qq=record.user_id,
                    reason=decision.reason,
                )
            except (KeyError, ValueError):
                rendered = fallback
            remark = sanitize_remark(decision.remark or rendered, fallback=fallback)

        params: dict[str, Any] = {
            "flag": record.flag,
            "approve": bool(decision.approve),
        }
        if decision.approve and remark:
            params["remark"] = remark

        try:
            response = await client.call("set_friend_add_request", params)
            ensure_action_ok(response, "set_friend_add_request")
        except Exception as exc:
            record.status = "error"
            record.last_error = str(exc)
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            self.ctx.logger.warning("处理好友申请失败: qq=%s error=%s", record.user_id, exc)
            if notify:
                await self._notify_error(record, str(exc))
            return False, f"处理失败：{exc}"

        status = "approved" if decision.approve else "rejected"
        self._finish_record(record, status, decision, remark=remark)
        self._register_decision()
        self._save_state()

        if decision.approve:
            self._friend_cache[record.user_id] = (True, time.time())
            await self._send_welcome(record.user_id)

        if notify:
            await self._notify_result(record, decision, remark)
        action_text = "同意" if decision.approve else "拒绝"
        return True, f"已{action_text} QQ {record.user_id} 的好友申请。"

    def _finish_record(
        self,
        record: FriendRequestRecord,
        status: str,
        decision: ReviewDecision,
        *,
        remark: str = "",
    ) -> None:
        """把记录移入已处理集合并保存。"""

        now = time.time()
        record.status = status
        record.decision = decision.to_dict()
        if remark:
            record.decision["applied_remark"] = remark

        item = record.to_dict()
        item["processed_at"] = now
        self._processed[record.flag or f"{record.user_id}:{int(now)}"] = item
        if record.flag:
            self._processed_flags.add(record.flag)
        self._pending.pop(record.user_id, None)
        self._stats[status] = self._stats.get(status, 0) + 1
        self._save_state()

    # ---------------------------------------------------------------- 通知与欢迎语

    async def _notify_result(self, record: FriendRequestRecord, decision: ReviewDecision, remark: str) -> None:
        """通知管理员自动处理结果。"""

        if decision.approve and not self.config.notify.notify_on_approve:
            return
        if not decision.approve and not self.config.notify.notify_on_reject:
            return
        icon = "✅" if decision.approve else "⛔"
        action = "已通过" if decision.approve else "已拒绝"
        lines = [f"{icon} {action}好友申请"]
        await self._notify_admin(
            self._build_notice_text(lines, record, decision, remark),
            avatar_qq=record.user_id,
        )

    async def _notify_manual(self, record: FriendRequestRecord, *, dry_run: bool = False) -> None:
        """通知管理员人工处理。"""

        if not self.config.notify.notify_on_manual:
            return
        title = "🧪 [演练] 好友申请待确认" if dry_run else "⚠️ 好友申请需要人工处理"
        lines = [title]
        await self._notify_admin(
            self._build_notice_text(lines, record, None, ""),
            avatar_qq=record.user_id,
        )
        await self._notify_admin(
            f"发送 /好友审批 同意 {record.user_id} [备注] 或 /好友审批 拒绝 {record.user_id} 处理。"
        )

    async def _notify_error(self, record: FriendRequestRecord, error: str) -> None:
        """通知管理员处理失败。"""

        if not self.config.notify.notify_on_error:
            return
        lines = ["❌ 好友申请处理失败", f"QQ：{record.user_id}", f"错误：{error[:300]}"]
        if record.comment:
            lines.append(f"验证消息：{record.comment[:200]}")
        lines.append("申请已保留，可修复后发送 /好友审批 重试 <QQ>。")
        await self._notify_admin("\n".join(lines), avatar_qq=record.user_id)

    def _build_notice_text(
        self,
        lines: list[str],
        record: FriendRequestRecord,
        decision: ReviewDecision | None,
        remark: str,
    ) -> str:
        """构造管理员通知正文。"""

        lines.append(f"QQ：{record.user_id}")
        profile = record.profile
        if profile is not None:
            if self.config.notify.include_profile:
                if profile.nickname:
                    lines.append(f"昵称：{profile.nickname}")
                detail_parts = [
                    f"性别 {sex_text(profile.sex)}",
                    f"年龄 {profile.age or '未知'}",
                    f"等级 {profile.level or '未知'}",
                ]
                lines.append("资料：" + "，".join(detail_parts))
                if profile.signature:
                    lines.append(f"签名：{profile.signature[:80]}")
                if profile.location:
                    lines.append(f"地区：{profile.location[:60]}")
                lines.append(f"已是好友：{'是' if profile.already_friend else '否'}")
            elif profile.nickname:
                lines.append(f"昵称：{profile.nickname}")
        if record.comment:
            lines.append(f"验证消息：{record.comment[:300]}")
        else:
            lines.append("验证消息：（空）")
        if decision is not None:
            lines.append(f"判断：{'通过' if decision.approve else '拒绝'}（置信度 {decision.confidence:.2f}）")
            lines.append(f"理由：{decision.reason[:300]}")
            if remark:
                lines.append(f"备注：{remark}")
            lines.append(f"来源：{decision.source}")
        return "\n".join(lines)[:1500]

    async def _notify_admin(self, text: str, *, exclude_qq: str = "", avatar_qq: str = "") -> None:
        """向管理员列表发送私聊通知。"""

        if not self.config.notify.enabled or not text.strip():
            return
        admins = normalize_qq_list(self.config.notify.admin_qqs)
        if not admins:
            self.ctx.logger.debug("未配置管理员 QQ，跳过通知: %s", text[:80])
            return
        for admin_qq in admins:
            if exclude_qq and admin_qq == exclude_qq:
                continue
            await self._send_private_message(admin_qq, text, avatar_qq=avatar_qq)

    async def _send_private_message(self, user_id: str, text: str, *, avatar_qq: str = "") -> bool:
        """通过 NapCat 发送私聊文本。"""

        client = self._client
        if client is None or not client.connected:
            self.ctx.logger.debug("NapCat 未连接，无法给 %s 发送通知", user_id)
            return False
        if not user_id.isdigit():
            return False

        segments: list[dict[str, Any]] = []
        if self.config.notify.include_avatar and avatar_qq.isdigit():
            segments.append(
                {
                    "type": "image",
                    "data": {"file": f"https://q1.qlogo.cn/g?b=qq&nk={avatar_qq}&s=640"},
                }
            )
        segments.append({"type": "text", "data": {"text": text}})

        try:
            response = await client.call(
                "send_private_msg",
                {"user_id": int(user_id), "message": segments},
            )
            ensure_action_ok(response, "send_private_msg")
            return True
        except Exception as exc:
            self.ctx.logger.warning("发送私聊通知失败: qq=%s error=%s", user_id, exc)
            return False

    async def _send_welcome(self, user_id: str) -> None:
        """发送欢迎语。"""

        messages = [str(item).strip() for item in self.config.strategy.welcome_messages if str(item).strip()]
        if not messages:
            return
        delay = max(0.0, float(self.config.strategy.welcome_delay_seconds))
        if delay:
            await asyncio.sleep(delay)
        for index, message in enumerate(messages):
            if index:
                await asyncio.sleep(0.4)
            sent = await self._send_private_message(user_id, message)
            if not sent:
                # NapCat 有时在同意申请后需要一点时间才允许私聊。
                await asyncio.sleep(1.2)
                await self._send_private_message(user_id, message)

    # ---------------------------------------------------------------- 命令

    @Command(
        "friend_gate_status",
        description="查看好友申请审批状态",
        pattern=r"^/好友审批(?:状态|\s+状态)?\s*$",
    )
    async def handle_status_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """处理状态查询命令。"""

        allowed, error_text = self._command_allowed(kwargs)
        if not allowed:
            return False, error_text, False

        pending = list(self._pending.values())
        pending_lines = []
        for item in pending[-5:]:
            pending_lines.append(
                f"- {item.get('user_id')}（{item.get('status', 'pending')}）"
                f"{(item.get('profile') or {}).get('nickname', '')}"
            )

        connection = "已连接" if self._client is not None and self._client.connected else "未连接"
        lines = [
            "好友申请审批状态",
            f"插件：{'启用' if self.config.plugin.enabled else '停用'}"
            f"{'（演练模式）' if self.config.plugin.dry_run else ''}",
            f"NapCat：{connection}",
            f"待处理：{len(pending)}",
            f"今日已处理：{self._count_today()}",
            f"累计：通过 {self._stats.get('approved', 0)}，拒绝 {self._stats.get('rejected', 0)}，"
            f"人工 {self._stats.get('manual', 0)}，跳过 {self._stats.get('skipped', 0)}",
        ]
        if pending_lines:
            lines.append("最近待处理：")
            lines.extend(pending_lines)
        else:
            lines.append("最近待处理：暂无")
        return True, "\n".join(lines), True

    @Command(
        "friend_gate_approve",
        description="管理员手动通过好友申请",
        pattern=r"^/好友审批\s*(?:同意|通过|approve)\s+(?P<target_qq>\d+)(?:\s+(?P<remark>.+?))?\s*$",
    )
    async def handle_approve_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """处理手动通过命令。"""

        allowed, error_text = self._command_allowed(kwargs)
        if not allowed:
            return False, error_text, False

        matched = kwargs.get("matched_groups") or {}
        target_qq = normalize_qq(matched.get("target_qq"))
        remark = str(matched.get("remark") or "").strip()
        if not target_qq:
            return True, "用法：/好友审批 同意 <QQ> [备注]", True

        record_data = self._pending.get(target_qq)
        if not isinstance(record_data, dict):
            return True, f"没有找到 QQ {target_qq} 的待处理申请。", True

        record = FriendRequestRecord.from_dict(record_data)
        if record is None:
            return True, f"QQ {target_qq} 的申请记录已损坏，请删除后重试。", True

        decision = ReviewDecision(
            approve=True,
            confidence=1.0,
            reason="管理员手动通过",
            remark=sanitize_remark(remark, fallback=record.profile.nickname if record.profile else ""),
            source="admin",
        )
        _success, message = await self._apply(record, decision, notify=False)
        return True, message, True

    @Command(
        "friend_gate_reject",
        description="管理员手动拒绝好友申请",
        pattern=r"^/好友审批\s*(?:拒绝|reject)\s+(?P<target_qq>\d+)\s*$",
    )
    async def handle_reject_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """处理手动拒绝命令。"""

        allowed, error_text = self._command_allowed(kwargs)
        if not allowed:
            return False, error_text, False

        matched = kwargs.get("matched_groups") or {}
        target_qq = normalize_qq(matched.get("target_qq"))
        if not target_qq:
            return True, "用法：/好友审批 拒绝 <QQ>", True

        record_data = self._pending.get(target_qq)
        if not isinstance(record_data, dict):
            return True, f"没有找到 QQ {target_qq} 的待处理申请。", True

        record = FriendRequestRecord.from_dict(record_data)
        if record is None:
            return True, f"QQ {target_qq} 的申请记录已损坏，请删除后重试。", True

        decision = ReviewDecision(
            approve=False,
            confidence=1.0,
            reason="管理员手动拒绝",
            source="admin",
        )
        _success, message = await self._apply(record, decision, notify=False)
        return True, message, True

    @Command(
        "friend_gate_retry",
        description="重新判断一条失败或转人工的好友申请",
        pattern=r"^/好友审批\s*(?:重试|retry)\s+(?P<target_qq>\d+)\s*$",
    )
    async def handle_retry_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """处理重试命令。"""

        allowed, error_text = self._command_allowed(kwargs)
        if not allowed:
            return False, error_text, False

        matched = kwargs.get("matched_groups") or {}
        target_qq = normalize_qq(matched.get("target_qq"))
        if not target_qq:
            return True, "用法：/好友审批 重试 <QQ>", True

        record_data = self._pending.get(target_qq)
        if not isinstance(record_data, dict):
            return True, f"没有找到 QQ {target_qq} 的待处理申请。", True

        record = FriendRequestRecord.from_dict(record_data)
        if record is None:
            return True, f"QQ {target_qq} 的申请记录已损坏，请删除后重试。", True

        try:
            record.profile = await self._fetch_profile(record.user_id)
            self._pending[record.user_id] = record.to_dict()
            self._save_state()
            decision = await self._decide(record)
            _success, message = await self._apply(record, decision, notify=False)
            return True, message, True
        except Exception as exc:
            return True, f"重试失败：{exc}", True

    def _command_allowed(self, kwargs: dict[str, Any]) -> tuple[bool, str | None]:
        """判断命令调用者是否允许操作。

        Returns:
            tuple[bool, str | None]: 是否允许，以及不允许时要返回的文本。
        """

        if bool(kwargs.get("is_local_operator")):
            return True, None
        if str(kwargs.get("group_id") or "").strip():
            return False, None
        sender_qq = normalize_qq(kwargs.get("user_id"))
        admins = set(normalize_qq_list(self.config.notify.admin_qqs))
        if not admins:
            return False, None
        if sender_qq and sender_qq in admins:
            return True, None
        return False, None

    # ---------------------------------------------------------------- 状态持久化

    def _load_state(self) -> None:
        """读取状态文件。"""

        path = self._state_path
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("读取好友审批状态失败: %s", exc)
            return
        if not isinstance(data, dict):
            return

        pending = data.get("pending")
        if isinstance(pending, dict):
            for key, value in pending.items():
                record = FriendRequestRecord.from_dict(value)
                if record is not None:
                    self._pending[str(key)] = record.to_dict()

        processed = data.get("processed")
        if isinstance(processed, dict):
            now = time.time()
            for key, value in processed.items():
                if not isinstance(value, dict):
                    continue
                processed_at = float(value.get("processed_at") or 0)
                if processed_at and now - processed_at > _PROCESSED_RETENTION_SECONDS:
                    continue
                self._processed[str(key)] = dict(value)
                if str(key):
                    self._processed_flags.add(str(key))

        decision_times = data.get("decision_times")
        if isinstance(decision_times, list):
            now = time.time()
            self._decision_times = [
                float(value)
                for value in decision_times
                if isinstance(value, (int, float)) and now - float(value) < _DECISION_TIME_RETENTION_SECONDS
            ]

        stats = data.get("stats")
        if isinstance(stats, dict):
            for key, value in stats.items():
                try:
                    self._stats[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

    def _save_state(self) -> None:
        """原子写入状态文件。"""

        path = self._state_path
        if path is None:
            return
        payload = {
            "version": _STATE_VERSION,
            "pending": self._pending,
            "processed": self._processed,
            "decision_times": self._decision_times[-1000:],
            "stats": self._stats,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
        except Exception as exc:
            self.ctx.logger.warning("保存好友审批状态失败: %s", exc)


def create_plugin() -> FriendGatePlugin:
    """创建插件实例。"""

    return FriendGatePlugin()
