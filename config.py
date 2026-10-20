"""好友申请智能审批插件的配置模型。"""

from __future__ import annotations

from typing import Any

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "user-check"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用好友申请自动审批")
    dry_run: bool = Field(
        default=False,
        description="演练模式：只做判断并通知管理员，不真正同意或拒绝申请",
    )
    timezone: str = Field(default="Asia/Shanghai", description="统计与日期使用的时区")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ConnectionConfig(PluginConfigBase):
    """NapCat 连接配置。"""

    __ui_label__ = "NapCat 连接"
    __ui_icon__ = "plug"
    __ui_order__ = 1

    ws_url: str = Field(
        default="",
        description="NapCat 正向 WebSocket 地址，例如 ws://127.0.0.1:7998；留空则自动读取 Napcat 适配器配置",
    )
    access_token: str = Field(
        default="",
        description="NapCat WebSocket 访问令牌；留空则自动读取 Napcat 适配器配置",
    )
    auto_discover: bool = Field(
        default=True,
        description="ws_url / access_token 留空时，自动从 plugins/Napcat-Adapter/config.toml 读取连接信息",
    )
    action_timeout_seconds: float = Field(
        default=15.0,
        ge=3.0,
        le=120.0,
        description="调用 NapCat 动作的超时时间（秒）",
    )
    reconnect_initial_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
        description="断线重连的初始等待时间（秒）",
    )
    reconnect_max_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description="断线重连的最大等待时间（秒）",
    )


class StrategyConfig(PluginConfigBase):
    """审批策略配置。"""

    __ui_label__ = "审批策略"
    __ui_icon__ = "scale"
    __ui_order__ = 2

    mode: str = Field(
        default="llm",
        description="处理模式：llm（模型判断）、manual（全部转人工）、auto_approve（无条件通过）",
    )
    model_name: str = Field(
        default="",
        description="指定模型名称；留空使用 task_name 对应的模型任务",
    )
    task_name: str = Field(default="utils", description="模型任务名，例如 utils / replyer / tool_use")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="模型温度")
    max_tokens: int = Field(default=600, ge=128, le=4096, description="模型最大输出 token")
    min_confidence: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="模型置信度低于该值时按 uncertain_action 处理",
    )
    uncertain_action: str = Field(
        default="manual",
        description="模型不确定或调用失败时的动作：manual（转人工）、reject（拒绝）、approve（通过）",
    )
    auto_remark: bool = Field(default=True, description="通过申请时是否自动设置好友备注")
    remark_template: str = Field(
        default="{nickname}",
        description="备注模板，可用变量：{nickname}、{qq}、{reason}",
    )
    welcome_messages: list[str] = Field(
        default_factory=lambda: ["你好呀，很高兴认识你～"],
        description="通过申请后发送的欢迎语列表，按顺序逐条发送",
    )
    welcome_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description="通过申请后等待多久再发欢迎语（秒）",
    )
    max_decisions_per_hour: int = Field(
        default=20,
        ge=1,
        le=500,
        description="每小时最多自动处理的申请数",
    )
    max_decisions_per_day: int = Field(
        default=100,
        ge=1,
        le=5000,
        description="每天最多自动处理的申请数",
    )


class FiltersConfig(PluginConfigBase):
    """名单与关键词过滤。"""

    __ui_label__ = "过滤规则"
    __ui_icon__ = "filter"
    __ui_order__ = 3

    whitelist_qqs: list[str] = Field(
        default_factory=list,
        description="白名单 QQ，命中后直接通过",
    )
    blacklist_qqs: list[str] = Field(
        default_factory=list,
        description="黑名单 QQ，命中后直接拒绝",
    )
    blacklist_keywords: list[str] = Field(
        default_factory=lambda: [
            "广告",
            "推广",
            "刷单",
            "返利",
            "博彩",
            "赌博",
            "贷款",
            "代练",
            "加群",
            "引流",
            "兼职",
            "色情",
            "裸聊",
            "约炮",
        ],
        description="验证消息中包含这些关键词时直接拒绝",
    )
    reject_empty_comment: bool = Field(
        default=False,
        description="验证消息为空时直接拒绝（默认交给模型判断）",
    )
    min_qq_level: int = Field(
        default=0,
        ge=0,
        le=200,
        description="最低 QQ 等级；0 表示不限制",
    )
    skip_if_already_friend: bool = Field(
        default=True,
        description="申请人已经是好友时跳过处理",
    )


class NotifyConfig(PluginConfigBase):
    """管理员通知配置。"""

    __ui_label__ = "管理员通知"
    __ui_icon__ = "bell"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否向管理员发送处理通知")
    admin_qqs: list[str] = Field(
        default_factory=list,
        description="接收通知和管理员命令的 QQ 号列表",
    )
    notify_on_approve: bool = Field(default=True, description="通过申请时通知管理员")
    notify_on_reject: bool = Field(default=True, description="拒绝申请时通知管理员")
    notify_on_manual: bool = Field(default=True, description="需要人工处理时通知管理员")
    notify_on_error: bool = Field(default=True, description="处理出错时通知管理员")
    include_profile: bool = Field(default=True, description="通知中附带申请人资料")
    include_avatar: bool = Field(default=False, description="通知中附带申请人头像")


class PromptConfig(PluginConfigBase):
    """提示词扩展配置。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-square"
    __ui_order__ = 5

    persona_override: str = Field(
        default="",
        description="自定义 Bot 人设；留空则读取主程序 personality 配置",
    )
    extra_criteria: str = Field(
        default="",
        description="追加给模型的审核标准，例如：只通过说明来意的申请",
    )


class FriendGateConfig(PluginConfigBase):
    """好友申请智能审批插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)


def normalize_qq(value: Any) -> str:
    """把任意 QQ 标识规范化为纯数字字符串。

    Args:
        value: 原始值。

    Returns:
        str: 纯数字 QQ；无法规范化时返回空字符串。
    """

    text = str(value or "").strip()
    if text.lower().startswith("qq:"):
        text = text[3:].strip()
    if text.startswith("o") or text.startswith("O"):
        text = text[1:]
    text = text.lstrip("0")
    return text if text.isdigit() else ""


def normalize_qq_list(values: Any) -> list[str]:
    """规范化 QQ 列表并去重。

    Args:
        values: 原始列表或单个值。

    Returns:
        list[str]: 规范化后的 QQ 列表。
    """

    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, (list, tuple, set)):
        candidates = list(values)
    else:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        qq = normalize_qq(item)
        if qq and qq not in seen:
            seen.add(qq)
            result.append(qq)
    return result
