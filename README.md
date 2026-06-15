# Perpvia 积分 Bot — 部署手册 & 指令速查
---

## 准备:3 样东西

1. 一个 Telegram 账号(你已有)
2. 一个 GitHub 账号(免费注册 github.com)
3. 一个 Railway 账号(免费注册 railway.app,用 GitHub 登录最快)

---

## 第一步:拿到 Bot Token(5分钟)

1. Telegram 里搜索 **@BotFather**,点开,发 `/newbot`
2. 按提示给 bot 起个名字(显示名,如 `Perpvia Points`)和用户名(必须以 bot 结尾,如 `PerpviaPointsBot`)
3. BotFather 会回你一串 **token**,形如 `7812345678:AAH...xyz`。**复制保存好,这就是 BOT_TOKEN。**
4. 再给同一个 BotFather 发 `/setprivacy` → 选你的 bot → 选 **Disable**。
   ⚠️ 这一步必须做!否则 bot 看不到群里的普通发言,发言计数就废了。

## 第二步:拿到你自己的 TG 数字ID(2分钟)

1. Telegram 搜 **@userinfobot**,点开发 `/start`
2. 它回你的 `Id`,是一串数字,如 `654321789`。**这就是 ADMIN_IDS。**
   (只有这个ID能用 /settle /award 等管理指令,别人用不了。)

## 第三步:把代码放上 GitHub(8分钟)

1. 登录 github.com,右上角 **+** → **New repository**
2. 名字随便填(如 `perpvia-bot`),选 **Private**(私有),点 **Create**
3. 进入空仓库页,点 **uploading an existing file**
4. 把我给你的 4 个文件全拖进去上传:`bot.py`、`requirements.txt`、`Procfile`、`README.md`
5. 点 **Commit changes**

## 第四步:在 Railway 部署(8分钟)

1. 登录 railway.app → **New Project** → **Deploy from GitHub repo**
2. 授权后选你刚建的 `perpvia-bot` 仓库
3. 部署会自动开始。这时去左侧 **Variables**(变量)标签,加两个变量:
   - `BOT_TOKEN` = 第一步那串 token
   - `ADMIN_IDS` = 第二步那串数字ID(多个管理员用英文逗号隔开)
4. **重要——加持久存储,否则重启丢数据:**
   - 在项目里点 **+ New** → **Volume**,挂载路径填 `/data`
   - 再加一个变量 `DB_PATH` = `/data/perpvia.db`
5. Railway 自动重新部署。看 **Deployments** 日志出现 `Perpvia bot 启动中...` 就成功了。

## 第五步:把 bot 拉进群,设管理员(2分钟)

1. 在你的 Telegram 群里 → 添加成员 → 搜你的 bot 用户名 → 加进来
2. 把 bot 设为**群管理员**(给"发送消息""管理消息"权限),否则它发不了榜单、建不了投票
3. 在群里发 `/count`,bot 回了人数 = 全部打通 ✅

---

## 你每天/每周只用这几条指令

### 用户会用的(自动,你不用管)
| 指令 | 作用 |
|---|---|
| `/checkin` | 用户签到 +5(连签7天额外+20),自动 |
| `/me` | 用户查自己积分 |
| `/rank` | 用户查自己排名 |
| `/invite` | 用户拿专属邀请链接,自动归因 |

### 你(管理员)每天用的
| 指令 | 什么时候用 | 例子 |
|---|---|---|
| `/predict 阶段 对阵 选项...` | 晚上开次日竞猜 | `/predict 小组赛 阿根廷vs法国 阿根廷胜 平局 法国胜` |
| `/settle 对阵 正确选项` | 比赛结果出来后报结果,bot自动给猜中者加分 | `/settle 阿根廷vs法国 阿根廷胜` |
| `/award @用户 分数` | 给截图/UGC/漏斗任务加分 | `/award @alice 30`(也可回复某条消息后发 `/award 30`) |
| `/top10` | 出总榜 | `/top10` |
| `/top10 week` | 出周榜 | `/top10 week` |

### 你每周一用的
| 指令 | 作用 |
|---|---|
| `/reset_week` | 周榜清零(总榜累计分不动),开启新一周 |
| `/export` | 导出CSV存档(结算周奖/总榜大奖时用) |
| `/count` | 看当前人数(对奖池解锁档位) |

---

## 你每天的实际操作流(≈15分钟)

1. **早上(2分钟)**:昨天比赛结果出了 → 发 `/settle 对阵名 结果`,bot 自动算完
2. **早上(8分钟)**:看"📸任务证明"话题的截图 → 高价值的(UGC/首充/首单)逐条 `/award`,推特关注这类抽查即可
3. **中午(3分钟)**:复制周历里写好的当日任务帖,发"📢官方公告"话题
4. **晚上(2分钟)**:发 `/top10`,把结果套日榜模板贴出去;同时发 `/predict` 开次日竞猜

签到、发言、邀请、积分计算、榜单——这些 bot 全自动,你一概不碰。

---

## 常见问题

**Q:重启会丢积分吗?** 不会,只要你做了第四步的 Volume + DB_PATH。没做才会丢。
**Q:bot 不记发言分?** 检查第一步的 `/setprivacy` 是否设成了 Disable,以及 bot 是不是群管理员。
**Q:`/award` 说找不到用户?** 该用户得先在群里发过言或 /start 过 bot,bot 才认得他。用"回复消息+/award 分数"最稳。
**Q:竞猜投票记不了分?** 代码里已强制 `is_anonymous=False`(非匿名),只要用 `/predict` 开的盘就没问题。别用 TG 自带的手动建投票。
**Q:免费额度够吗?** Railway 免费额度跑一个轻量bot一个月基本够;若提示额度,升级最低档约 $5/月。
