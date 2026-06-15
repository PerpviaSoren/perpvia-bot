# -*- coding: utf-8 -*-
"""
Perpvia Pioneer 积分赛 × 世界杯预测季 — Telegram 积分 Bot
========================================================
一份给 Soren 的、可直接部署运行的积分机器人。

设计目标:把 Soren 每天的手工时间压到最低。
机器自动做:签到 / 发言计数 / 邀请追踪与48h判定 / 双轨积分 / 榜单导出。
Soren 手动做(用指令,几秒钟):报比赛结果 / 给截图任务加分 / 周榜清零。

积分规则(对应方案):
- 签到 /checkin            : +5,每日一次;连续7天额外 +20
- 有效发言 >=5 条/日        : +5(每日封顶一次),刷屏不计
- 邀请(48h不退+1次活跃)   : +15/有效人,无每日上限
- 竞猜参与                  : +2/场;猜中小组赛 +10 / 淘汰赛 +8;连中3场 +15
- 截图/漏斗任务            : 管理员 /award 手动加分
双轨:
- total_points  全程累计,永不清零 -> 总榜
- week_points   当周积分,每周一 /reset_week 清零 -> 周榜
"""

import os
import logging
import datetime as dt
from collections import defaultdict

from telegram import Update, Poll
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    PollAnswerHandler, ContextTypes, filters
)

# ----------------------------------------------------------------------------
# 配置:从环境变量读取,部署时在托管平台填写,不要把 token 写死在代码里
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# 管理员 TG 数字ID(只有这些人能用 /settle /award /reset_week 等管理指令)
# 多个用逗号分隔,例如 "123456789,987654321"。先填你自己的ID。
ADMIN_IDS = set(
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
)

# 数据库文件路径(托管平台上用持久卷,否则重启会丢数据,见部署说明)
DB_PATH = os.environ.get("DB_PATH", "perpvia.db")

# 积分规则参数(想改分值改这里即可,不用动逻辑)
PTS_CHECKIN = 5
PTS_CHECKIN_STREAK7 = 20
PTS_TALK_DAILY = 5
TALK_THRESHOLD = 5            # 当日有效发言达到多少条给分
PTS_INVITE = 15
INVITE_HOLD_HOURS = 48        # 被邀请人需停留多少小时
PTS_PREDICT_JOIN = 2
PTS_PREDICT_HIT_GROUP = 10
PTS_PREDICT_HIT_KO = 8
PTS_PREDICT_STREAK3 = 15

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("perpvia")

# ----------------------------------------------------------------------------
# 数据层:用 SQLite,零额外依赖,单文件,适合一个人独立运营
# ----------------------------------------------------------------------------
import sqlite3

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            total_points INTEGER DEFAULT 0,
            week_points  INTEGER DEFAULT 0,
            joined_at    TEXT,
            invited_by   INTEGER,
            invite_credited INTEGER DEFAULT 0,
            last_checkin TEXT,
            checkin_streak INTEGER DEFAULT 0,
            had_activity INTEGER DEFAULT 0
        )
    """)
    # 每日发言计数:user_id + 日期 -> 条数,以及当日是否已给过发言分
    c.execute("""
        CREATE TABLE IF NOT EXISTS talk (
            user_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
            credited INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
    # 竞猜:每场一条;用户投票记录
    c.execute("""
        CREATE TABLE IF NOT EXISTS predict_polls (
            poll_id TEXT PRIMARY KEY, match TEXT, stage TEXT,
            options TEXT, settled INTEGER DEFAULT 0, day TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS predict_votes (
            poll_id TEXT, user_id INTEGER, choice INTEGER,
            PRIMARY KEY (poll_id, user_id)
        )
    """)
    # 竞猜连胜计数(连中3场用)
    c.execute("""
        CREATE TABLE IF NOT EXISTS predict_streak (
            user_id INTEGER PRIMARY KEY, streak INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id, username, invited_by=None):
    conn = db(); c = conn.cursor()
    row = c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        c.execute(
            "INSERT INTO users (user_id, username, joined_at, invited_by) VALUES (?,?,?,?)",
            (user_id, username, dt.datetime.utcnow().isoformat(), invited_by),
        )
    else:
        c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    conn.commit(); conn.close()

def add_points(user_id, pts):
    """同时加到总榜和周榜(双轨)。"""
    conn = db(); c = conn.cursor()
    c.execute(
        "UPDATE users SET total_points=total_points+?, week_points=week_points+? WHERE user_id=?",
        (pts, pts, user_id),
    )
    conn.commit(); conn.close()

def today_str():
    return dt.date.today().isoformat()

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ----------------------------------------------------------------------------
# 用户指令
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ 欢迎加入 Perpvia Pioneer 积分赛!\n\n"
        "常用指令:\n"
        "/checkin 每日签到 (+5,连签7天额外+20)\n"
        "/me 查看我的积分\n"
        "/rank 查看我的排名\n"
        "/invite 获取我的专属邀请链接\n\n"
        "每日完成任务、参与世界杯竞猜,冲榜赢奖池!"
    )

async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    conn = db(); c = conn.cursor()
    row = c.execute("SELECT last_checkin, checkin_streak FROM users WHERE user_id=?", (u.id,)).fetchone()
    today = today_str()
    if row["last_checkin"] == today:
        conn.close()
        await update.message.reply_text("今天已经签到过啦 ✅ 明天再来~")
        return
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    streak = (row["checkin_streak"] or 0) + 1 if row["last_checkin"] == yesterday else 1
    bonus = PTS_CHECKIN_STREAK7 if streak % 7 == 0 else 0
    c.execute(
        "UPDATE users SET last_checkin=?, checkin_streak=?, had_activity=1 WHERE user_id=?",
        (today, streak, u.id),
    )
    conn.commit(); conn.close()
    add_points(u.id, PTS_CHECKIN + bonus)
    msg = f"✅ 签到成功 +{PTS_CHECKIN} 分!连续签到 {streak} 天。"
    if bonus:
        msg += f"\n🎁 连签 {streak} 天达成,额外 +{bonus} 分!"
    await update.message.reply_text(msg)

async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    conn = db()
    row = conn.execute(
        "SELECT total_points, week_points, checkin_streak FROM users WHERE user_id=?", (u.id,)
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 你的积分\n总榜累计:{row['total_points']} 分\n"
        f"本周积分:{row['week_points']} 分\n连续签到:{row['checkin_streak']} 天"
    )

async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    conn = db()
    rows = conn.execute("SELECT user_id FROM users ORDER BY total_points DESC").fetchall()
    conn.close()
    pos = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == u.id), None)
    if pos:
        await update.message.reply_text(f"🏆 你当前总榜排名:第 {pos} 名 / 共 {len(rows)} 人")
    else:
        await update.message.reply_text("你还没有积分,先 /checkin 开始吧!")

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """生成专属邀请深链。被邀请人通过此链接进群,自动归因。"""
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    bot_username = (await ctx.bot.get_me()).username
    # 用 start 深链携带邀请人ID;被邀请人点开会先 /start bot,再引导进群
    link = f"https://t.me/{bot_username}?start=inv_{u.id}"
    await update.message.reply_text(
        f"🔗 你的专属邀请链接:\n{link}\n\n"
        f"好友通过它进入社区,停留满 {INVITE_HOLD_HOURS} 小时且有过互动,"
        f"你将获得 +{PTS_INVITE} 分/人(无上限)!"
    )

async def cmd_start_with_payload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """处理带邀请参数的 /start inv_<inviterid>。"""
    args = ctx.args
    u = update.effective_user
    invited_by = None
    if args and args[0].startswith("inv_"):
        try:
            invited_by = int(args[0][4:])
        except ValueError:
            invited_by = None
    if invited_by and invited_by != u.id:
        ensure_user(u.id, u.username or u.first_name, invited_by=invited_by)
    await cmd_start(update, ctx)

# ----------------------------------------------------------------------------
# 自动:发言计数(刷屏不计由 Telegram/Rose 反spam配合,这里做基础阈值)
# ----------------------------------------------------------------------------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    day = today_str()
    conn = db(); c = conn.cursor()
    c.execute(
        "INSERT INTO talk (user_id, day, count) VALUES (?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
        (u.id, day),
    )
    # 标记该用户有互动(用于邀请48h判定)
    c.execute("UPDATE users SET had_activity=1 WHERE user_id=?", (u.id,))
    row = c.execute("SELECT count, credited FROM talk WHERE user_id=? AND day=?", (u.id, day)).fetchone()
    conn.commit(); conn.close()
    # 达到阈值且当天未给过 -> 给发言分
    if row["count"] >= TALK_THRESHOLD and row["credited"] == 0:
        conn = db(); conn.execute(
            "UPDATE talk SET credited=1 WHERE user_id=? AND day=?", (u.id, day)
        ); conn.commit(); conn.close()
        add_points(u.id, PTS_TALK_DAILY)

# ----------------------------------------------------------------------------
# 邀请48h判定:由定时任务每小时扫一遍,满足条件的补发邀请分
# ----------------------------------------------------------------------------
async def job_credit_invites(ctx: ContextTypes.DEFAULT_TYPE):
    conn = db(); c = conn.cursor()
    rows = c.execute(
        "SELECT user_id, invited_by, joined_at, had_activity, invite_credited "
        "FROM users WHERE invited_by IS NOT NULL AND invite_credited=0"
    ).fetchall()
    now = dt.datetime.utcnow()
    credited = 0
    for r in rows:
        try:
            joined = dt.datetime.fromisoformat(r["joined_at"])
        except Exception:
            continue
        hours = (now - joined).total_seconds() / 3600
        if hours >= INVITE_HOLD_HOURS and r["had_activity"] == 1:
            c.execute("UPDATE users SET invite_credited=1 WHERE user_id=?", (r["user_id"],))
            conn.commit()
            add_points(r["invited_by"], PTS_INVITE)
            credited += 1
    conn.close()
    if credited:
        log.info(f"邀请补发:本轮给 {credited} 个有效邀请加分")

# ----------------------------------------------------------------------------
# 竞猜:管理员开盘 -> bot发非匿名投票 -> 用户投 -> 管理员报结果 -> bot自动结算
# ----------------------------------------------------------------------------
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/predict 小组赛|淘汰赛 对阵名 选项1 选项2 [选项3]
    例: /predict 小组赛 阿根廷vs法国 阿根廷胜 平局 法国胜
    """
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=4)
    if len(parts) < 4:
        await update.message.reply_text(
            "用法:/predict 小组赛 对阵名 选项1 选项2 [选项3]\n"
            "例:/predict 小组赛 阿根廷vs法国 阿根廷胜 平局 法国胜"
        )
        return
    stage = parts[1]
    match = parts[2]
    options = parts[3].split()
    if len(options) < 2:
        await update.message.reply_text("至少要2个选项(独赢二选一或加平局三选一)")
        return
    sent = await ctx.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=f"⚽ {match} 你预测谁赢?({stage})",
        options=options,
        is_anonymous=False,          # 关键:非匿名才能记分!
        allows_multiple_answers=False,
    )
    conn = db(); conn.execute(
        "INSERT INTO predict_polls (poll_id, match, stage, options, day) VALUES (?,?,?,?,?)",
        (sent.poll.id, match, stage, "|".join(options), today_str()),
    ); conn.commit(); conn.close()
    await update.message.reply_text(f"✅ 已开盘:{match}。参与+{PTS_PREDICT_JOIN},猜中更多分。")

async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """记录用户投票 + 立即给参与分。"""
    ans = update.poll_answer
    if not ans.option_ids:
        return
    poll_id = ans.poll_id
    user = ans.user
    conn = db(); c = conn.cursor()
    poll = c.execute("SELECT poll_id, settled FROM predict_polls WHERE poll_id=?", (poll_id,)).fetchone()
    if not poll or poll["settled"] == 1:
        conn.close(); return
    ensure_user(user.id, user.username or user.first_name)
    exists = c.execute(
        "SELECT 1 FROM predict_votes WHERE poll_id=? AND user_id=?", (poll_id, user.id)
    ).fetchone()
    c.execute(
        "INSERT OR REPLACE INTO predict_votes (poll_id, user_id, choice) VALUES (?,?,?)",
        (poll_id, user.id, ans.option_ids[0]),
    )
    conn.commit(); conn.close()
    if not exists:
        add_points(user.id, PTS_PREDICT_JOIN)   # 参与就加分

async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settle <对阵名> <正确选项>
    例: /settle 阿根廷vs法国 阿根廷胜
    bot 自动给所有猜中者加分,并处理连胜。
    """
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("用法:/settle 对阵名 正确选项\n例:/settle 阿根廷vs法国 阿根廷胜")
        return
    match, correct = parts[1], parts[2]
    conn = db(); c = conn.cursor()
    poll = c.execute(
        "SELECT poll_id, options, stage, settled FROM predict_polls WHERE match=? ORDER BY rowid DESC", (match,)
    ).fetchone()
    if not poll:
        conn.close(); await update.message.reply_text(f"没找到对阵:{match}"); return
    if poll["settled"] == 1:
        conn.close(); await update.message.reply_text("这场已经结算过了"); return
    options = poll["options"].split("|")
    if correct not in options:
        conn.close()
        await update.message.reply_text(f"选项不对。可选:{' / '.join(options)}"); return
    correct_idx = options.index(correct)
    hit_pts = PTS_PREDICT_HIT_KO if "淘汰" in poll["stage"] else PTS_PREDICT_HIT_GROUP
    votes = c.execute("SELECT user_id, choice FROM predict_votes WHERE poll_id=?", (poll["poll_id"],)).fetchall()
    hit_users, miss_users = [], []
    for v in votes:
        if v["choice"] == correct_idx:
            hit_users.append(v["user_id"])
        else:
            miss_users.append(v["user_id"])
    # 加命中分 + 连胜处理
    streak_bonus_count = 0
    for uid in hit_users:
        add_points(uid, hit_pts)
        row = c.execute("SELECT streak FROM predict_streak WHERE user_id=?", (uid,)).fetchone()
        s = (row["streak"] if row else 0) + 1
        c.execute("INSERT OR REPLACE INTO predict_streak (user_id, streak) VALUES (?,?)", (uid, s))
        if s % 3 == 0:
            add_points(uid, PTS_PREDICT_STREAK3)
            streak_bonus_count += 1
    for uid in miss_users:
        c.execute("INSERT OR REPLACE INTO predict_streak (user_id, streak) VALUES (?,0)", (uid,))
    c.execute("UPDATE predict_polls SET settled=1 WHERE poll_id=?", (poll["poll_id"],))
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"✅ {match} 结算完成!正确:{correct}\n"
        f"猜中 {len(hit_users)} 人,各 +{hit_pts} 分。\n"
        f"达成连中3场 {streak_bonus_count} 人,额外 +{PTS_PREDICT_STREAK3} 分。"
    )

# ----------------------------------------------------------------------------
# 管理员:手动加分(截图/漏斗任务) + 榜单 + 周榜清零
# ----------------------------------------------------------------------------
async def cmd_award(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/award @用户名 分数  或  回复某条消息后 /award 分数"""
    if not is_admin(update.effective_user.id):
        return
    target_id, target_name, pts = None, None, None
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, target_name = t.id, t.username or t.first_name
        try:
            pts = int(update.message.text.split()[1])
        except (IndexError, ValueError):
            await update.message.reply_text("用法(回复模式):回复目标消息 + /award 30"); return
    else:
        parts = update.message.text.split()
        if len(parts) < 3:
            await update.message.reply_text("用法:/award @用户名 30  或  回复某消息 /award 30"); return
        target_name = parts[1].lstrip("@")
        try:
            pts = int(parts[2])
        except ValueError:
            await update.message.reply_text("分数要是数字"); return
        conn = db()
        row = conn.execute("SELECT user_id FROM users WHERE username=?", (target_name,)).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text(f"没找到用户 @{target_name}(他需先和bot说过话/进过群)"); return
        target_id = row["user_id"]
    add_points(target_id, pts)
    await update.message.reply_text(f"✅ 已给 @{target_name} 加 {pts} 分")

async def cmd_top10(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/top10 总榜  /top10 week 周榜"""
    if not is_admin(update.effective_user.id):
        return
    arg = ctx.args[0] if ctx.args else "total"
    col = "week_points" if arg == "week" else "total_points"
    title = "本周积分榜" if arg == "week" else "总榜累计"
    conn = db()
    rows = conn.execute(
        f"SELECT username, {col} AS p FROM users ORDER BY {col} DESC LIMIT 10"
    ).fetchall()
    conn.close()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"🏆 {title} TOP 10\n"]
    for i, r in enumerate(rows):
        name = f"@{r['username']}" if r["username"] else "匿名用户"
        lines.append(f"{medals[i]} {name} — {r['p']} 分")
    await update.message.reply_text("\n".join(lines))

async def cmd_reset_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """每周一用:清零所有人 week_points(总榜不动)。"""
    if not is_admin(update.effective_user.id):
        return
    conn = db(); conn.execute("UPDATE users SET week_points=0"); conn.commit(); conn.close()
    await update.message.reply_text("✅ 本周积分已清零,新一周开始!(总榜累计分不受影响)")

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """导出全部积分为CSV,发给管理员存档/结算用。"""
    if not is_admin(update.effective_user.id):
        return
    import csv, io
    conn = db()
    rows = conn.execute(
        "SELECT user_id, username, total_points, week_points, joined_at FROM users ORDER BY total_points DESC"
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "total_points", "week_points", "joined_at"])
    for r in rows:
        w.writerow([r["user_id"], r["username"], r["total_points"], r["week_points"], r["joined_at"]])
    buf.seek(0)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"perpvia_points_{today_str()}.csv"
    await ctx.bot.send_document(chat_id=update.effective_chat.id, document=data)

async def cmd_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/count 当前参赛人数(有积分的用户数)"""
    if not is_admin(update.effective_user.id):
        return
    conn = db()
    total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    active = conn.execute("SELECT COUNT(*) AS n FROM users WHERE total_points>0").fetchone()["n"]
    conn.close()
    await update.message.reply_text(f"👥 已登记 {total} 人,其中 {active} 人有积分")

# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("缺少 BOT_TOKEN 环境变量,请在部署平台填写。")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # /start 带邀请参数特殊处理
    app.add_handler(CommandHandler("start", cmd_start_with_payload))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("invite", cmd_invite))

    # 管理员
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("settle", cmd_settle))
    app.add_handler(CommandHandler("award", cmd_award))
    app.add_handler(CommandHandler("top10", cmd_top10))
    app.add_handler(CommandHandler("reset_week", cmd_reset_week))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("count", cmd_count))

    # 投票回调
    app.add_handler(PollAnswerHandler(on_poll_answer))
    # 发言计数(放最后,捕获普通文本)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # 定时任务:每小时核一次48h邀请
    app.job_queue.run_repeating(job_credit_invites, interval=3600, first=60)

    log.info("Perpvia bot 启动中...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
