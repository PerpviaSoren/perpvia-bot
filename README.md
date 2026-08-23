# PerpVia Telegram Bot

这是 PerpVia 社区积分体系 Telegram Bot，基于 PRD V1.0 实现。Bot 用于在官方 Telegram 群内记录用户活跃行为，统计每周积分，验证邀请关系，生成排行榜，并按周计算合约体验金奖励名单。

Bot 使用 Telegram `user_id` 作为用户唯一身份标识，所以用户修改 `username` 不会影响积分、邀请关系或奖励记录。

> 语言说明：本 README 与项目运维说明使用中文；Telegram Bot 面向用户的消息、指令菜单及管理员回执统一使用英文。

## 指令菜单与使用范围

用户在 Telegram 输入 `/` 时，Bot 会按照账号和聊天位置展示指令及英文备注：

- 普通用户在私聊和官方活动群中只看到用户指令。
- `ADMIN_IDS` 中的账号在私聊和官方活动群中会看到用户指令及完整管理员指令。
- 管理员指令不会只依赖菜单隐藏，执行时仍会再次校验 Telegram user ID。
- 私聊窗口允许使用 Bot 指令，但普通私聊消息不会计入社区聊天积分。
- 群聊中仅 `GROUP_CHAT_ID` 配置的官方群可以使用 Bot。
- 其他群聊不展示本 Bot 的指令菜单，也不能执行 Bot 指令、记录消息或获得积分。

为了让管理员在官方群中获得个人专属指令菜单，Bot 需要是该群管理员，并具备 Telegram 要求的限制成员权限。邀请链接功能还需要创建邀请链接权限。

## 这个 Bot 是什么

PerpVia Telegram Bot 是一个社区增长与活跃激励工具，主要负责：

- 记录用户在官方 Telegram 群内的有效聊天行为。
- 为有效聊天消息自动发放积分。
- 为用户生成个人专属邀请链接。
- 验证被邀请用户是否在规定时间内完成有效发言。
- 为有效邀请自动发放积分。
- 按 UTC+0 世界标准时间的每周周期统计积分。
- 展示用户本周积分、分类积分、门槛差距和排行榜。
- 每周结算后计算符合条件用户的奖励金额。
- 生成待管理员复核的奖励名单。
- 导出积分、邀请和奖励相关 CSV 报表。
- 支持管理员手动加减分、屏蔽异常用户、配置活动参数。
- 提供周期状态、运营统计、用户详情、邀请记录与结算预览。
- 记录管理员操作日志与风控信号，便于复核和追踪。

## 核心活动规则

- 积分周期：默认 7 天，UTC+0，每周一 00:00 至周日 23:59。
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
10. 管理员可以使用 `/admin_settle_preview` 查看只读结算预览。
11. 管理员使用 `/admin_publish_rewards` 发布正式奖励名单。

正式发布前，Bot 会按照最新的 blocked 状态和当前群成员状态重新计算奖励，防止结算后新增的风控状态被遗漏。奖励公告成功发送到官方群后，奖励记录才会更新为 `published`。

## 用户指令

| 指令 | 功能 |
| --- | --- |
| `/perpvia` | 查看活动简介和基础指令，替代容易与其他 Bot 重复的 `/start`。 |
| `/rules` | 查看积分规则、奖励规则和风控说明，回复文案可由管理员配置。 |
| `/points` | 查看当前周期、本周总积分、聊天积分、邀请积分、调整积分、距离奖励门槛还差多少，以及今日积分上限状态。 |
| `/invite` | 生成或查看个人专属邀请链接。 |
| `/rank` | 查看本周排行榜 Top 20 和自己的排名。 |

## 管理员指令

| 指令 | 功能 |
| --- | --- |
| `/admin_whoami` | 显示当前 Telegram user ID，并检查该账号是否匹配 `ADMIN_IDS`。此命令可用于排查管理员权限。 |
| `/admin_help` | 查看所有管理员命令。 |
| `/admin_set_perpvia <文案>` | 设置 `/perpvia` 的英文回复文案；也可以回复一条文本消息执行该命令，以保留多行格式。 |
| `/admin_set_perpvia reset` | 恢复 `/perpvia` 默认回复文案。 |
| `/admin_set_rules <文案>` | 设置 `/rules` 的英文回复文案；也可以回复一条文本消息执行该命令。 |
| `/admin_set_rules reset` | 恢复 `/rules` 默认回复文案。 |
| `/admin_adjust @username 10 [reason]` | 给指定用户增加 10 Points。 |
| `/admin_adjust @username -10 [reason]` | 给指定用户减少 10 Points。 |
| 回复用户消息后发送 `/admin_adjust 10 [reason]` | 给被回复的用户增加 10 Points。 |
| 回复用户消息后发送 `/admin_adjust -10 [reason]` | 给被回复的用户减少 10 Points。 |
| `/admin_export_points [cycle_id]` | 导出用户积分汇总 CSV 和积分流水明细 CSV。 |
| `/admin_export_invites` | 导出邀请关系 CSV。 |
| `/admin_export_rewards [cycle_id]` | 导出奖励名单 CSV。 |
| `/admin_publish_rewards [cycle_id]` | 将复核后的奖励名单发布到官方群。 |
| `/admin_settle_preview [cycle_id]` | 只读预览奖励资格、预计金额和排除原因，不修改数据。 |
| `/admin_cycle` | 查看当前周期、剩余时间以及近期周期状态。 |
| `/admin_stats [cycle_id]` | 查看积分、消息、邀请、达标人数和风控队列统计。 |
| `/admin_user @username` | 查看指定用户的身份、积分、消息、邀请和风控摘要。 |
| `/admin_invites [@username]` | 查看最近邀请记录，可按邀请人或被邀请人筛选。 |
| `/admin_config` | 查看当前可配置参数。 |
| `/admin_config set <key> <value>` | 修改指定配置项。 |
| `/admin_block @username [reason]` | 将用户标记为 pending review，记录原因并排除出奖励名单。 |
| `/admin_unblock @username [reason]` | 完成复核并恢复用户的奖励资格。 |
| `/admin_risks [@username]` | 查看待处理风控信号。 |
| `/admin_risk_resolve <flag_id>` | 将指定风控信号标记为已处理。 |

## 加固与优化

当前版本已加入以下上线前保护：

- 为积分、邀请、消息审计、奖励和风控高频查询建立数据库索引。
- 启动时校验必填环境变量及所有活动配置，非法配置会阻止 Bot 启动。
- `/admin_config` 会校验数值范围和配置之间的关系。
- 当前周期已经产生积分后，不允许在线修改周期时间、积分规则、奖励门槛或奖池。
- 每个周期保存奖励门槛与奖池快照，后续配置变更不会改写历史周期的奖励规则。
- 已冻结周期拒绝新增积分流水。
- 当前周期不能提前发布奖励，只有已结算周期可执行正式发布。
- 管理员加减分、封禁、解封、配置修改、风控处理和奖励发布均写入审计日志。
- blocked 用户的新邀请及 Bot 账号邀请会被拒绝；自邀请、Bot 邀请和短时间邀请异常会生成风控信号。
- 正式奖励发布前重新检查用户状态，公告发送失败时不会错误标记为已发布。
- Telegram 指令菜单自动注册为英文，并有统一的异常日志处理。

用户体验同步优化：

- `/points` 显示周期剩余时间、积分分类、今日上限和邀请进度。
- `/points` 的周期范围不显示时区后缀，后台计分统一采用 UTC+0。
- `/rank` 清晰显示 `Qualified`、`Pending review` 以及用户距离门槛的积分差距。
- `/rules` 按周期、聊天、邀请、内容审核和奖励分段展示。
- 所有用户可见文案统一使用英文。

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
- `admin_actions`：管理员操作审计表。
- `risk_flags`：风控信号及处理状态表。

## 配置说明

必填环境变量：

- `BOT_TOKEN`：Telegram Bot Token。
- `ADMIN_IDS`：管理员 Telegram user_id，必须填写纯数字 ID，不能填写 `@username`。多个 ID 支持英文逗号、空格或 JSON 数组格式，例如 `123456789,987654321`。
- `GROUP_CHAT_ID`：官方 Telegram 群 ID。

`GROUP_CHAT_ID` 必须填写 Telegram 群的数字 ID，超级群通常是以 `-100` 开头的负数。部署后修改 `ADMIN_IDS` 或 `GROUP_CHAT_ID` 必须重启 Bot，Bot 才会重新注册对应的指令菜单作用域。

可以复制项目中的 `.env.example` 作为配置清单。该示例不包含真实 Token；实际 `.env` 已加入 `.gitignore`，不要将生产密钥提交到代码仓库。

可选环境变量：

- `GROUP_USERNAME`：官方群公开用户名，可带或不带 `@`。
- `DB_PATH`：SQLite 数据库路径，默认 `perpvia.db`。
- `GOOGLE_FORM_URL`：领奖信息收集表单链接。
- `PERPVIA_REPLY_TEXT`：可选，初始化 `/perpvia` 自定义英文文案。运行后可用管理员命令修改。
- `RULES_REPLY_TEXT`：可选，初始化 `/rules` 自定义英文文案。运行后可用管理员命令修改。
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

周期时间、积分规则、奖励门槛和奖池只能在当前周期尚未产生积分时修改。推荐在活动上线前完成主要参数配置，开始计分后仅调整敏感词、重复检测窗口或表单地址等非计分参数。

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

SQLite 数据库及其 WAL 文件已加入 `.gitignore`。生产环境需要将 `DB_PATH` 指向持久化磁盘，并定期备份数据库文件，避免平台重启或重新部署导致积分数据丢失。

## 本地语法检查

不安装 Telegram 依赖时，也可以先做 Python 语法检查：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/pycache python3 -m py_compile bot.py
```

如果需要在本机完整运行 Bot，需要先安装 `requirements.txt` 中的依赖，并配置 `BOT_TOKEN`、`ADMIN_IDS`、`GROUP_CHAT_ID` 等环境变量。
