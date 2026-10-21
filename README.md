# 好友申请智能审批（MaiBot 插件）

让麦麦自己看好友申请：读取申请人的公开资料和验证消息，由模型判断是否通过；
模型不确定或调用失败时，按配置转人工、拒绝或通过。通过后可以自动设置备注、
发送欢迎语，并把处理结果私聊通知管理员。

> 适用平台：NapCat（Napcat 适配器）。插件通过 NapCat 的正向 WebSocket 直接接收
> `request.friend` 事件，因此不需要在 NapCat 里额外配置 HTTP POST 回调。

## 功能

- **模型审核**：结合 QQ 等级、性别、年龄、签名、所在地、注册时间、验证消息等公开信息判断。
- **免打扰时段**：指定时间内收到申请只暂存，不作任何回应；时段结束后自动重新处理。
- **防提示词注入**：验证消息被明确标记为不可信内容；要求模型忽略“忽略规则”“输出提示词”“转账”等指令。
- **白黑名单与关键词**：白名单直接通过，黑名单 / 广告关键词直接拒绝。
- **置信度阈值**：模型置信度低于 `min_confidence` 时按 `uncertain_action` 兜底。
- **人工兜底**：管理员可在私聊中用命令同意 / 拒绝 / 重试待处理申请。
- **自动备注与欢迎语**：通过后自动设置好友备注，并按顺序发送欢迎语。
- **频率限制**：每小时 / 每天自动处理数量可配置，避免被批量申请刷爆。
- **通知与状态**：管理员私聊收到处理结果；`/好友审批状态` 查看待处理和统计。
- **演练模式**：`dry_run = true` 时只判断、不执行，适合先观察模型判断是否靠谱。

## 安装

1. 在 MaiBot WebUI 的插件市场安装，或把本仓库克隆/解压到 `MaiBot/plugins/` 下。
2. 确认 `Napcat_Adapter` 已安装并正常工作。
3. 重载插件或重启 MaiBot。
4. 第一次加载后，在 WebUI 的插件配置里填写管理员 QQ 等信息。

插件会自动读取 `plugins/Napcat-Adapter/config.toml` 中的：

```toml
[napcat_server]
host = "127.0.0.1"
port = 7998
token = "..."
```

如果自动发现失败（例如适配器目录名不同、NapCat 在别的机器上），在插件配置里手动填写：

```toml
[connection]
ws_url = "ws://127.0.0.1:7998"
access_token = "你的 NapCat token"
auto_discover = false
```

> NapCat 的 WebSocket 服务允许同时连接多个客户端。MaiBot 适配器占用一个连接，
> 本插件会再建一个只读事件 + 动作调用的连接，不会影响正常聊天。

## 配置说明

### `[plugin]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用自动审批 |
| `dry_run` | `false` | 演练模式：只判断并通知，不真正同意 / 拒绝 |
| `timezone` | `Asia/Shanghai` | 每日统计使用的时区 |

### `[connection]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `ws_url` | 空 | NapCat 正向 WS 地址；留空自动读取适配器配置 |
| `access_token` | 空 | NapCat token；留空自动读取 |
| `auto_discover` | `true` | 是否自动发现 |
| `action_timeout_seconds` | `15` | 单次动作超时 |
| `reconnect_initial_seconds` | `2` | 初始重连等待 |
| `reconnect_max_seconds` | `60` | 最大重连等待 |

### `[strategy]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `mode` | `llm` | `llm` / `manual` / `auto_approve` |
| `model_name` | 空 | 指定模型；留空用 `task_name` 任务 |
| `task_name` | `utils` | 模型任务名 |
| `temperature` | `0.2` | 温度 |
| `max_tokens` | `600` | 最大输出 token |
| `min_confidence` | `0.65` | 低于该置信度按兜底处理 |
| `uncertain_action` | `manual` | `manual` / `reject` / `approve` |
| `auto_remark` | `true` | 通过后自动设置备注 |
| `remark_template` | `{nickname}` | 备注模板，支持 `{nickname}` / `{qq}` / `{reason}` |
| `welcome_messages` | `["你好呀，很高兴认识你～"]` | 通过后逐条发送 |
| `max_decisions_per_hour` | `20` | 每小时自动处理上限 |
| `max_decisions_per_day` | `100` | 每日自动处理上限 |

### `[filters]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `whitelist_qqs` | `[]` | 白名单，直接通过 |
| `blacklist_qqs` | `[]` | 黑名单，直接拒绝 |
| `blacklist_keywords` | 广告/推广/刷单... | 验证消息命中即拒绝 |
| `reject_empty_comment` | `false` | 空验证消息是否直接拒绝 |
| `min_qq_level` | `0` | 最低 QQ 等级，0 不限制 |
| `skip_if_already_friend` | `true` | 已经是好友时跳过 |

### `[notify]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否通知管理员 |
| `admin_qqs` | `[]` | 管理员 QQ，**必填**才能用命令和收通知 |
| `notify_on_approve` / `notify_on_reject` | `true` | 结果通知 |
| `notify_on_manual` / `notify_on_error` | `true` | 人工 / 错误通知 |
| `include_profile` | `true` | 通知附带资料 |
| `include_avatar` | `false` | 通知附带头像 |

### `[quiet_hours]`

在免打扰时段内收到的好友申请只会**暂存**，不判断、不通知、不同意/拒绝；时段结束后后台任务会自动重新取出处理。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用免打扰时段 |
| `start` | `00:00` | 开始时间 HH:MM |
| `end` | `08:00` | 结束时间 HH:MM，可跨午夜 |
| `timezone` | `Asia/Shanghai` | 时区 |
| `check_interval_seconds` | `60` | 结束后检查暂存申请的间隔 |

### `[prompt]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `persona_override` | 空 | 自定义人设；留空读取主程序人格 |
| `extra_criteria` | 空 | 追加审核标准 |

## 管理员命令

命令只在私聊生效，且发送者必须在 `notify.admin_qqs` 中（本机控制台操作员除外）。

| 命令 | 说明 |
| --- | --- |
| `/好友审批状态` | 查看连接、免打扰状态、待处理、今日与累计统计 |
| `/好友审批 同意 <QQ> [备注]` | 手动通过 |
| `/好友审批 拒绝 <QQ>` | 手动拒绝 |
| `/好友审批 重试 <QQ>` | 重新读取资料并判断 |

## 安全建议

- 第一次使用先开 `dry_run = true`，观察几天模型判断，再关掉演练。
- 建议配置 `blacklist_keywords` 和 `max_decisions_per_day`，防止批量广告。
- 模型可能判断错误；重要的账号建议把 `uncertain_action` 保持为 `manual`。
- 插件不会把申请人的手机号、邮箱交给模型。
- 通过申请后欢迎语会直接私聊对方，请只在欢迎语中放公开信息。

## 常见问题

**收不到申请？**

1. 确认 NapCat 的 WebSocket 服务已开启（适配器能正常聊天就说明已开启）。
2. 查看插件日志是否显示“已连接 NapCat WebSocket”。
3. 检查 `ws_url` / `access_token` 是否与 NapCat 配置一致。
4. 本插件不需要在 NapCat 里配置 HTTP 客户端。

**提示 NapCat 动作超时？**

把 `action_timeout_seconds` 调大，或检查 NapCat 是否卡顿。

**模型一直转人工？**

检查 `model_name` / `task_name` 是否可用，以及 `min_confidence` 是否过高。

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

## License

MIT
