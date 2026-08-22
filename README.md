# PerpVia Telegram Bot

这是 PerpVia 社区积分体系 Telegram Bot，基于 PRD V1.0 实现。Bot 用于在官方 Telegram 群内记录用户活跃行为，统计每周积分，验证邀请关系，生成排行榜，并按周计算合约体验金奖励名单。

Bot 使用 Telegram `user_id` 作为用户唯一身份标识，所以用户修改 `username` 不会影响积分、邀请关系或奖励记录。

## 这个 Bot 是什么

PerpVia Telegram Bot 是一个社区增长与活跃激励工具，主要负责：

- 记录用户在官方 Telegram 群内的有效聊天行为。
- 为有效聊天消息自动发放积分。
- 为用户生成个人专属邀请链接。
- 验证被邀请用户是否在规定时间内完成有效发言。
- 为有效邀请自动发放积分。
- 按 UTC+8 的每周周期统计积分。
- 展示用户本周积分、分类积分、门槛差距和排行榜。
- 每周结算后计算符合条件用户的奖励金额。
- 生成待管理员复核的奖励名单。
- 导出积分、邀请和奖励相关 CSV 报表。
- 支持管理员手动加减分、屏蔽异常用户、配置活动参数。

## 核心活动规则

- 积分周期：默认 7 天，UTC+8，每周一 00:00 至周日 23:59。
- 聊天积分：每条有效消息 +2 Points。
- 聊天上限：每位用户每日最多获得 20 聊天 Points。
- 邀请积分：每个有效邀请 +10 Points。
- 邀请上限：每位用户每日最多获得 50 邀请 Points。
- 邀请有效期：被邀请用户需要通过专属邀请链接入群，并在 3 天内发送 1 条有效消息。
- 奖励门槛：用户本周积分达到 100 Points 才具备奖励资格。
- 奖励池：默认每周 300U 合约体验金。
- 奖励计算方式：

```text
用户奖励 = 用户本周积分 / 所有达标用户本周总积分 * 本周奖励池
```

奖励金额保留 1 位小数。奖励会先进入 `pending_review` 状态，管理员复核并发布后才作为正式奖励名单。

## 有效聊天消息规则

一条消息需要同时满足以下条件，才会被计为有效消息：

- 来自活动 Telegram 群内的真实用户。
- 不是 Bot 发送的消息。
- 是普通文本消息，不是命令。
- 默认不少于 15 个非空白字符。
- 不是转发消息。
- 不是纯链接。
- 不是纯表情或无意义字符堆叠。
- 没有命中配置中的敏感词、广告词或诈骗词。
- 不是同一用户在短时间内重复发送的刷屏内容。
- 用户当天聊天积分尚未达到上限。

无效消息不会获得积分，但会记录到 `message_audit` 表中，方便后续排查原因。

## 邀请计分流程

1. 用户输入 `/invite`。
2. Bot 创建或返回该用户的个人专属邀请链接。
3. 新用户通过该邀请链接加入官方 Telegram 群。
4. Bot 将邀请关系记录为 `pending`。
5. 被邀请用户需要在 3 天内发送 1 条有效消息。
6. 条件满足后，邀请关系变为 `valid`。
7. 邀请人获得 10 Points，但每日邀请积分最多 50 Points。

如果邀请人当天邀请积分已达到上限，该邀请仍可被记录为有效邀请，但不会额外发放积分，也不会顺延到之后日期补发。

## 每周结算流程

1. Bot 定时检查是否有已结束但未结算的积分周期。
2. 周期结束后，上一周期会被结算并冻结。
3. 系统汇总用户本周期积分。
4. 筛选本周积分不少于 100 Points 的用户。
5. 排除已被管理员标记为 blocked 或 pending review 的用户。
6. 排除已经不在官方 Telegram 群内的用户。
7. 根据达标用户积分占比计算奖励金额。
8. 生成 `pending_review` 状态的奖励记录。
9. 管理员导出奖励名单并进行人工复核。
10. 管理员使用 `/admin_publish_rewards` 发布正式奖励名单。

## 用户指令

| 指令 | 功能 |
| --- | --- |
| `/start` | 查看活动简介和基础指令。 |
| `/rules` | 查看积分规则、奖励规则和风控说明。 |
| `/points` | 查看当前周期、本周总积分、聊天积分、邀请积分、调整积分、距离奖励门槛还差多少，以及今日积分上限状态。 |
| `/invite` | 生成或查看个人专属邀请链接。 |
| `/rank` | 查看本周排行榜 Top 20 和自己的排名。 |

## 管理员指令

| 指令 | 功能 |
| --- | --- |
| `/admin_adjust @username 10 [reason]` | 给指定用户增加 10 Points。 |
| `/admin_adjust @username -10 [reason]` | 给指定用户减少 10 Points。 |
| 回复用户消息后发送 `/admin_adjust 10 [reason]` | 给被回复的用户增加 10 Points。 |
| 回复用户消息后发送 `/admin_adjust -10 [reason]` | 给被回复的用户减少 10 Points。 |
| `/admin_export_points [cycle_id]` | 导出用户积分汇总 CSV 和积分流水明细 CSV。 |
| `/admin_export_invites` | 导出邀请关系 CSV。 |
| `/admin_export_rewards [cycle_id]` | 导出奖励名单 CSV。 |
| `/admin_publish_rewards [cycle_id]` | 将复核后的奖励名单发布到官方群。 |
| `/admin_config` | 查看当前可配置参数。 |
| `/admin_config set <key> <value>` | 修改指定配置项。 |
| `/admin_block @username` | 将用户标记为 blocked / pending review，使其不能进入奖励名单。 |
| `/admin_unblock @username` | 解除用户的 blocked / pending review 状态。 |

## 数据模型

Bot 会自动创建并维护以下主要数据表：

- `settings`：活动配置项。
- `users`：Telegram 用户信息、入群状态、邀请来源、风控状态。
- `cycles`：积分周期的开始时间、结束时间和结算状态。
- `point_events`：积分流水表，记录 chat、invite、adjust 等所有积分变化。
- `invite_links`：用户个人专属邀请链接。
- `invites`：邀请关系表，记录 pending、valid、expired、rejected 等状态。
- `rewards`：奖励表，记录周期、用户积分、奖励金额和发布状态。
- `message_audit`：消息审计表，记录有效和无效消息及原因。

## 配置说明

必填环境变量：

- `BOT_TOKEN`：Telegram Bot Token。
- `ADMIN_IDS`：管理员 Telegram user_id，多个 ID 用英文逗号分隔。
- `GROUP_CHAT_ID`：官方 Telegram 群 ID。

可选环境变量：

- `GROUP_USERNAME`：官方群公开用户名，可带或不带 `@`。
- `DB_PATH`：SQLite 数据库路径，默认 `perpvia.db`。
- `GOOGLE_FORM_URL`：领奖信息收集表单链接。
- `CYCLE_LENGTH_DAYS`：积分周期长度，默认 `7`。
- `CYCLE_ANCHOR_DATE`：周期锚点日期，默认 `2026-08-17`。
- `CYCLE_START_HOUR`：周期开始小时，默认 `0`。
- `CYCLE_START_MINUTE`：周期开始分钟，默认 `0`。
- `VALID_MESSAGE_MIN_CHARS`：有效消息最少字符数，默认 `15`。
- `CHAT_POINTS_PER_MESSAGE`：单条有效消息积分，默认 `2`。
- `DAILY_CHAT_POINTS_CAP`：每日聊天积分上限，默认 `20`。
- `INVITE_POINTS`：单个有效邀请积分，默认 `10`。
- `DAILY_INVITE_POINTS_CAP`：每日邀请积分上限，默认 `50`。
- `INVITE_VALID_DAYS`：邀请有效期，默认 `3`。
- `WEEKLY_REWARD_THRESHOLD`：周奖励门槛，默认 `100`。
- `WEEKLY_REWARD_POOL`：周奖励池，默认 `300`。
- `MIN_SECONDS_BETWEEN_VALID_MESSAGES`：同一用户有效消息最小间隔，默认 `0`。
- `DUPLICATE_MESSAGE_WINDOW_MINUTES`：重复消息检测窗口，默认 `360`。
- `SENSITIVE_WORDS`：敏感词列表，使用英文逗号分隔。

这些配置会初始化到 `settings` 表中。管理员可以使用以下指令在线修改：

```text
/admin_config set <key> <value>
```

## 部署说明

依赖文件为 `requirements.txt`：

```text
python-telegram-bot[job-queue]==21.6
```

进程启动文件为 `Procfile`：

```text
worker: python bot.py
```

常见部署平台会自动读取 `requirements.txt` 安装依赖，并根据 `Procfile` 启动 worker 进程。

## 本地语法检查

不安装 Telegram 依赖时，也可以先做 Python 语法检查：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/pycache python3 -m py_compile bot.py
```

如果需要在本机完整运行 Bot，需要先安装 `requirements.txt` 中的依赖，并配置 `BOT_TOKEN`、`ADMIN_IDS`、`GROUP_CHAT_ID` 等环境变量。
