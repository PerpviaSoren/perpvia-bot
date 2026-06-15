# -*- coding: utf-8 -*-
"""
Perpvia Pioneer Points Race x World Cup Prediction Season - Telegram Points Bot
================================================================================
A ready-to-deploy points bot for Soren. All user-facing text is in English.

Goal: minimize Soren's daily manual work.
Automatic: check-in / message counting / invite tracking & 48h validation /
           dual-track points / leaderboard export.
Manual (commands, seconds): report match results / award screenshot tasks /
           weekly reset.

Scoring rules:
- Check-in /checkin        : +5, once a day; +20 bonus every 7-day streak
- Active chat >=5 msgs/day  : +5 (once per day max), spam not counted
- Invite (48h stay + 1 activity): +15 per valid user, no daily cap
- Prediction join           : +2/match; correct group stage +10 / knockout +8;
                              3 correct in a row +15
- Screenshot/funnel tasks   : admin /award manual points
Dual-track:
- total_points  cumulative, never reset -> overall leaderboard
- week_points   weekly, reset every Monday via /reset_week -> weekly leaderboard
"""

import os
import logging
import datetime as dt

from telegram import Update, Poll
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    PollAnswerHandler, ContextTypes, filters
)

# ----------------------------------------------------------------------------
# Config: read from environment variables. Set these on your hosting platform.
# Never hard-code the token in the file.
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Admin TG numeric IDs (only these can use /settle /award /reset_week etc.)
# Comma-separated, e.g. "123456789,987654321". Put your own ID first.
ADMIN_IDS = set(
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
)

# Public group username WITHOUT the @ (e.g. if your group is t.me/perpvia,
# set GROUP_USERNAME=perpvia). Used to build invite links that open the group.
GROUP_USERNAME = os.environ.get("GROUP_USERNAME", "").lstrip("@")

# Database file path (use a persistent volume on the host, otherwise data is
# lost on restart - see deployment notes).
DB_PATH = os.environ.get("DB_PATH", "perpvia.db")

# Scoring parameters (change values here, no need to touch logic)
PTS_CHECKIN = 5
PTS_CHECKIN_STREAK7 = 20
PTS_TALK_DAILY = 5
TALK_THRESHOLD = 5            # messages/day needed to earn the chat points
PTS_INVITE = 15
INVITE_HOLD_HOURS = 48        # hours an invited user must stay
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
# Data layer: SQLite, zero extra dependencies, single file.
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS talk (
            user_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
            credited INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS predict_streak (
            user_id INTEGER PRIMARY KEY, streak INTEGER DEFAULT 0
        )
    """)
    # Pending invites captured from group-join deep links, awaiting the user
    # to actually appear (handled when they first send a message / are seen).
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_invites (
            invitee_id INTEGER PRIMARY KEY, inviter_id INTEGER, ts TEXT
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id, username, invited_by=None):
    conn = db(); c = conn.cursor()
    row = c.execute("SELECT user_id, invited_by FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        c.execute(
            "INSERT INTO users (user_id, username, joined_at, invited_by) VALUES (?,?,?,?)",
            (user_id, username, dt.datetime.utcnow().isoformat(), invited_by),
        )
    else:
        c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        # Backfill inviter if we learn it later and it wasn't set before
        if invited_by and not row["invited_by"]:
            c.execute("UPDATE users SET invited_by=? WHERE user_id=?", (invited_by, user_id))
    conn.commit(); conn.close()

def add_points(user_id, pts):
    """Add to both total and weekly (dual-track)."""
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
# User commands
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Welcome to the Perpvia Pioneer Points Race!\n\n"
        "Common commands:\n"
        "/checkin - Daily check-in (+5, +20 bonus on a 7-day streak)\n"
        "/me - View my points\n"
        "/rank - View my ranking\n"
        "/invite - Get my personal invite link\n\n"
        "Complete daily tasks, join the World Cup predictions, "
        "and climb the leaderboard to win the prize pool!"
    )

async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    conn = db(); c = conn.cursor()
    row = c.execute("SELECT last_checkin, checkin_streak FROM users WHERE user_id=?", (u.id,)).fetchone()
    today = today_str()
    if row["last_checkin"] == today:
        conn.close()
        await update.message.reply_text("You've already checked in today ✅ Come back tomorrow!")
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
    msg = f"✅ Check-in successful! +{PTS_CHECKIN} points. Current streak: {streak} day(s)."
    if bonus:
        msg += f"\n🎁 {streak}-day streak reached - extra +{bonus} points!"
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
        f"📊 Your points\n"
        f"Overall total: {row['total_points']} pts\n"
        f"This week: {row['week_points']} pts\n"
        f"Check-in streak: {row['checkin_streak']} day(s)"
    )

async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    conn = db()
    rows = conn.execute("SELECT user_id FROM users ORDER BY total_points DESC").fetchall()
    conn.close()
    pos = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == u.id), None)
    if pos:
        await update.message.reply_text(f"🏆 Your overall rank: #{pos} of {len(rows)} participants")
    else:
        await update.message.reply_text("You don't have any points yet. Start with /checkin!")

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate a personal invite link that opens the public GROUP and carries
    the inviter's ID so the bot can attribute the invite."""
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    if GROUP_USERNAME:
        # startgroup deep link: opens the bot's "add to group" / join flow for
        # the public group while carrying the inviter id as payload.
        link = f"https://t.me/{GROUP_USERNAME}?start=inv_{u.id}"
        # Note: for a public group, the cleanest user flow is a direct group
        # link. We also expose a tracking link below.
        track = f"https://t.me/{(await ctx.bot.get_me()).username}?start=inv_{u.id}"
        await update.message.reply_text(
            f"🔗 Your personal invite link:\n{link}\n\n"
            f"(Tracking link, ensures you get credit:\n{track})\n\n"
            f"When a friend joins through your link, stays for {INVITE_HOLD_HOURS}h "
            f"and interacts at least once, you earn +{PTS_INVITE} points per friend "
            f"(no cap)!"
        )
    else:
        # Fallback: bot-start tracking link only
        track = f"https://t.me/{(await ctx.bot.get_me()).username}?start=inv_{u.id}"
        await update.message.reply_text(
            f"🔗 Your personal invite link:\n{track}\n\n"
            f"When a friend joins through it, stays for {INVITE_HOLD_HOURS}h and "
            f"interacts at least once, you earn +{PTS_INVITE} points per friend (no cap)!"
        )

async def cmd_start_with_payload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /start inv_<inviterid> - records the invite relationship."""
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
        # also stash as pending in case the user row already existed
        conn = db()
        conn.execute(
            "INSERT OR IGNORE INTO pending_invites (invitee_id, inviter_id, ts) VALUES (?,?,?)",
            (u.id, invited_by, dt.datetime.utcnow().isoformat()),
        )
        conn.commit(); conn.close()
        # And direct them to the group
        if GROUP_USERNAME:
            await update.message.reply_text(
                f"✅ You're in! Join the community here 👉 https://t.me/{GROUP_USERNAME}\n\n"
                "Then come back and use /checkin to start earning points."
            )
            return
    await cmd_start(update, ctx)

# ----------------------------------------------------------------------------
# New member tracking: when someone joins the group, attribute pending invite.
# ----------------------------------------------------------------------------
async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fires on member status changes. When a new member joins, if we have a
    pending invite for them, set invited_by."""
    res = update.chat_member
    if not res:
        return
    new = res.new_chat_member
    old = res.old_chat_member
    # Detect a fresh join (was left/kicked/none -> member)
    joined = (
        old.status in ("left", "kicked") and new.status in ("member", "administrator", "creator")
    )
    if not joined:
        return
    uid = new.user.id
    uname = new.user.username or new.user.first_name
    conn = db(); c = conn.cursor()
    p = c.execute("SELECT inviter_id FROM pending_invites WHERE invitee_id=?", (uid,)).fetchone()
    conn.close()
    inviter = p["inviter_id"] if p else None
    ensure_user(uid, uname, invited_by=inviter)

# ----------------------------------------------------------------------------
# Automatic: message counting (spam exclusion handled with Rose/Telegram)
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
    c.execute("UPDATE users SET had_activity=1 WHERE user_id=?", (u.id,))
    row = c.execute("SELECT count, credited FROM talk WHERE user_id=? AND day=?", (u.id, day)).fetchone()
    conn.commit(); conn.close()
    if row["count"] >= TALK_THRESHOLD and row["credited"] == 0:
        conn = db(); conn.execute(
            "UPDATE talk SET credited=1 WHERE user_id=? AND day=?", (u.id, day)
        ); conn.commit(); conn.close()
        add_points(u.id, PTS_TALK_DAILY)

# ----------------------------------------------------------------------------
# Invite 48h validation: hourly job credits valid invites
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
        log.info(f"Invite credit: awarded {credited} valid invite(s) this round")

# ----------------------------------------------------------------------------
# Predictions
# ----------------------------------------------------------------------------
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/predict group|knockout <match> <opt1> <opt2> [opt3]
    e.g. /predict group ARGvsFRA ARG_win Draw FRA_win
    Stage words containing 'knockout' or 'ko' use the knockout point value.
    """
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=4)
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /predict <stage> <match> <opt1> <opt2> [opt3]\n"
            "e.g. /predict group ARGvsFRA ARG_win Draw FRA_win"
        )
        return
    stage = parts[1]
    match = parts[2]
    options = parts[3].split()
    if len(options) < 2:
        await update.message.reply_text("Need at least 2 options (2-way win or 3-way with draw).")
        return
    sent = await ctx.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=f"⚽ {match} - who will win? ({stage})",
        options=options,
        is_anonymous=False,          # MUST be non-anonymous to score!
        allows_multiple_answers=False,
    )
    conn = db(); conn.execute(
        "INSERT INTO predict_polls (poll_id, match, stage, options, day) VALUES (?,?,?,?,?)",
        (sent.poll.id, match, stage, "|".join(options), today_str()),
    ); conn.commit(); conn.close()
    await update.message.reply_text(
        f"✅ Poll opened: {match}. Voting +{PTS_PREDICT_JOIN}, correct guess earns more."
    )

async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        add_points(user.id, PTS_PREDICT_JOIN)

async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settle <match> <correct_option>
    e.g. /settle ARGvsFRA ARG_win
    """
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /settle <match> <correct_option>\ne.g. /settle ARGvsFRA ARG_win")
        return
    match, correct = parts[1], parts[2]
    conn = db(); c = conn.cursor()
    poll = c.execute(
        "SELECT poll_id, options, stage, settled FROM predict_polls WHERE match=? ORDER BY rowid DESC", (match,)
    ).fetchone()
    if not poll:
        conn.close(); await update.message.reply_text(f"No poll found for match: {match}"); return
    if poll["settled"] == 1:
        conn.close(); await update.message.reply_text("This match has already been settled."); return
    options = poll["options"].split("|")
    if correct not in options:
        conn.close()
        await update.message.reply_text(f"Invalid option. Choose from: {' / '.join(options)}"); return
    correct_idx = options.index(correct)
    stage_l = poll["stage"].lower()
    is_ko = ("knockout" in stage_l) or ("ko" in stage_l) or ("淘汰" in poll["stage"])
    hit_pts = PTS_PREDICT_HIT_KO if is_ko else PTS_PREDICT_HIT_GROUP
    votes = c.execute("SELECT user_id, choice FROM predict_votes WHERE poll_id=?", (poll["poll_id"],)).fetchall()
    hit_users, miss_users = [], []
    for v in votes:
        if v["choice"] == correct_idx:
            hit_users.append(v["user_id"])
        else:
            miss_users.append(v["user_id"])
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
        f"✅ {match} settled! Correct answer: {correct}\n"
        f"{len(hit_users)} correct, +{hit_pts} pts each.\n"
        f"{streak_bonus_count} hit a 3-in-a-row streak, +{PTS_PREDICT_STREAK3} bonus."
    )

# ----------------------------------------------------------------------------
# Admin: manual award + leaderboards + weekly reset
# ----------------------------------------------------------------------------
async def cmd_award(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/award @username points  OR  reply to a message + /award points"""
    if not is_admin(update.effective_user.id):
        return
    target_id, target_name, pts = None, None, None
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, target_name = t.id, t.username or t.first_name
        try:
            pts = int(update.message.text.split()[1])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage (reply mode): reply to a message + /award 30"); return
    else:
        parts = update.message.text.split()
        if len(parts) < 3:
            await update.message.reply_text("Usage: /award @username 30  OR  reply to a message /award 30"); return
        target_name = parts[1].lstrip("@")
        try:
            pts = int(parts[2])
        except ValueError:
            await update.message.reply_text("Points must be a number."); return
        conn = db()
        row = conn.execute("SELECT user_id FROM users WHERE username=?", (target_name,)).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text(
                f"User @{target_name} not found (they must have messaged the bot / joined the group first)."
            ); return
        target_id = row["user_id"]
    add_points(target_id, pts)
    await update.message.reply_text(f"✅ Awarded {pts} points to @{target_name}.")

async def cmd_top10(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/top10 overall  |  /top10 week weekly"""
    if not is_admin(update.effective_user.id):
        return
    arg = ctx.args[0] if ctx.args else "total"
    col = "week_points" if arg == "week" else "total_points"
    title = "Weekly Leaderboard" if arg == "week" else "Overall Leaderboard"
    conn = db()
    rows = conn.execute(
        f"SELECT username, {col} AS p FROM users ORDER BY {col} DESC LIMIT 10"
    ).fetchall()
    conn.close()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"🏆 {title} - TOP 10\n"]
    for i, r in enumerate(rows):
        name = f"@{r['username']}" if r["username"] else "Anonymous"
        lines.append(f"{medals[i]} {name} - {r['p']} pts")
    await update.message.reply_text("\n".join(lines))

async def cmd_reset_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Monday: reset everyone's week_points (overall total unaffected)."""
    if not is_admin(update.effective_user.id):
        return
    conn = db(); conn.execute("UPDATE users SET week_points=0"); conn.commit(); conn.close()
    await update.message.reply_text(
        "✅ Weekly points reset. A new week begins! (Overall totals are unaffected.)"
    )

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Export all points as CSV to the admin."""
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
    """/count current participant numbers"""
    if not is_admin(update.effective_user.id):
        return
    conn = db()
    total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    active = conn.execute("SELECT COUNT(*) AS n FROM users WHERE total_points>0").fetchone()["n"]
    conn.close()
    await update.message.reply_text(f"👥 {total} registered, {active} with points.")

# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable. Set it on your host.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start_with_payload))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("invite", cmd_invite))

    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("settle", cmd_settle))
    app.add_handler(CommandHandler("award", cmd_award))
    app.add_handler(CommandHandler("top10", cmd_top10))
    app.add_handler(CommandHandler("reset_week", cmd_reset_week))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("count", cmd_count))

    app.add_handler(PollAnswerHandler(on_poll_answer))
    # Track joins for invite attribution
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    # Message counting (last, catches plain text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_repeating(job_credit_invites, interval=3600, first=60)

    log.info("Perpvia bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
