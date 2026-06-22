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
# set GROUP_USERNAME=perpvia). Used as a fallback link.
GROUP_USERNAME = os.environ.get("GROUP_USERNAME", "").lstrip("@")

# Group numeric chat ID (REQUIRED for native named invite links).
# How to get it: add the bot to the group as admin, then in the group send
# /chatid  -> the bot replies with the ID (a negative number like -1001234567890).
# Put that number here as the GROUP_CHAT_ID environment variable.
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID) if GROUP_CHAT_ID else None
except ValueError:
    GROUP_CHAT_ID = None

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

import threading
_db_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn

def db_execute(sql, params=()):
    """Thread-safe single execute + commit. Use for all writes."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            r = conn.execute(sql, params)
            conn.commit()
            return r
        finally:
            conn.close()

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
            options TEXT, settled INTEGER DEFAULT 0, day TEXT,
            deadline TEXT, chat_id INTEGER, message_id INTEGER, closed INTEGER DEFAULT 0
        )
    """)
    # Safe migrations for DBs created before these columns existed
    for col, decl in [("deadline","TEXT"),("chat_id","INTEGER"),
                      ("message_id","INTEGER"),("closed","INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE predict_polls ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
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
    # Native named invite links: maps each created link to the inviter.
    c.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY, inviter_id INTEGER, created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id, username, invited_by=None):
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            c = conn.cursor()
            row = c.execute("SELECT user_id, invited_by FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                c.execute(
                    "INSERT INTO users (user_id, username, joined_at, invited_by) VALUES (?,?,?,?)",
                    (user_id, username, dt.datetime.utcnow().isoformat(), invited_by),
                )
            else:
                c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
                if invited_by and not row["invited_by"]:
                    c.execute("UPDATE users SET invited_by=? WHERE user_id=?", (invited_by, user_id))
            conn.commit()
        finally:
            conn.close()

def add_points(user_id, pts):
    """Add to both total and weekly (dual-track). Thread-safe."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "UPDATE users SET total_points=total_points+?, week_points=week_points+? WHERE user_id=?",
                (pts, pts, user_id),
            )
            conn.commit()
        finally:
            conn.close()

def today_str():
    return dt.date.today().isoformat()

def norm(s):
    """Normalize a string for forgiving matching: drop emoji/symbols/spaces,
    lowercase. So '🇪🇸ESPvs🇨🇻CPV', 'ESPvsCPV', 'esp vs cpv' all match."""
    if s is None:
        return ""
    out = []
    for ch in s:
        # keep letters and digits only (ascii + unicode letters), drop the rest
        if ch.isalnum() and not (0x1F000 <= ord(ch) <= 0x1FAFF) and not (0x1F1E6 <= ord(ch) <= 0x1F1FF):
            out.append(ch.lower())
    return "".join(out)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def in_scoring_group(update):
    """True only if the update happens in the configured scoring group.
    Earning actions (check-in/talk/invite/predict-vote) are valid ONLY there.
    If GROUP_CHAT_ID is not set, fall back to allowing (so the bot still works
    before configuration)."""
    if not GROUP_CHAT_ID:
        return True
    chat = update.effective_chat
    return chat is not None and chat.id == GROUP_CHAT_ID

# ----------------------------------------------------------------------------
# User commands
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    group_line = (
        f"\n⚠️ Points are only earned inside our official group: https://t.me/{GROUP_USERNAME}\n"
        if GROUP_USERNAME else
        "\n⚠️ Points are only earned inside our official community group.\n"
    )
    await update.message.reply_text(
        "⚽ Welcome to the Perpvia Pioneer Points Race!\n\n"
        "Here in private chat you can check your progress:\n"
        "/me - View my points\n"
        "/rank - View my ranking\n"
        "/invite - Get my personal invite link\n"
        + group_line +
        "To EARN points (check-in, chat, predictions), do it in the group:\n"
        "/checkin - Daily check-in (+5, +20 bonus on a 7-day streak)\n\n"
        "Complete daily tasks, join the World Cup predictions, "
        "and climb the leaderboard to win the prize pool!"
    )

async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    # Earning only allowed in the designated group
    if not in_scoring_group(update):
        await update.message.reply_text(
            "📍 Check-in only counts in the official community group.\n"
            "👉 Join and check in here: " + (f"https://t.me/{GROUP_USERNAME}" if GROUP_USERNAME else "the Perpvia Pioneer Hub")
        )
        return
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
    """Create a NATIVE named group invite link for this user via Telegram API.
    Friends who click it join the group directly; Telegram tells the bot which
    link they used, so attribution is automatic and accurate."""
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)

    # If we've already made a link for this user, reuse it (one link per user).
    conn = db()
    existing = conn.execute(
        "SELECT invite_link FROM invite_links WHERE inviter_id=?", (u.id,)
    ).fetchone()
    conn.close()
    if existing:
        await update.message.reply_text(
            f"🔗 Your personal invite link:\n{existing['invite_link']}\n\n"
            f"Friends who join through it, stay {INVITE_HOLD_HOURS}h and interact "
            f"at least once earn you +{PTS_INVITE} points each (no cap)!"
        )
        return

    if not GROUP_CHAT_ID:
        await update.message.reply_text(
            "⚠️ Invite links aren't configured yet. Admin: set GROUP_CHAT_ID "
            "(use /chatid in the group) and give the bot 'Invite Users via Link' permission."
        )
        return

    try:
        # name lets you see who owns the link in Telegram's admin UI too
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            name=f"inv_{u.id}_{u.username or u.first_name}"[:32],
            creates_join_request=False,   # direct join (your group is open-join)
        )
    except Exception as e:
        log.error(f"create invite link failed: {e}")
        await update.message.reply_text(
            "⚠️ Couldn't create your link. The bot may be missing the "
            "'Invite Users via Link' admin permission in the group."
        )
        return

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO invite_links (invite_link, inviter_id, created_at) VALUES (?,?,?)",
        (link_obj.invite_link, u.id, dt.datetime.utcnow().isoformat()),
    )
    conn.commit(); conn.close()

    await update.message.reply_text(
        f"🔗 Your personal invite link:\n{link_obj.invite_link}\n\n"
        f"Friends who join through it, stay {INVITE_HOLD_HOURS}h and interact "
        f"at least once earn you +{PTS_INVITE} points each (no cap)!"
    )


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin helper: print this chat's numeric ID so you can set GROUP_CHAT_ID."""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        f"This chat's ID is: {update.effective_chat.id}\n"
        f"Set this as the GROUP_CHAT_ID environment variable."
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
    """Fires on member status changes. When a new member joins, attribute the
    invite using the native invite link they joined through (Telegram provides
    res.invite_link). Falls back to any pending start-link invite."""
    res = update.chat_member
    if not res:
        return
    new = res.new_chat_member
    old = res.old_chat_member
    joined = (
        old.status in ("left", "kicked") and new.status in ("member", "administrator", "creator")
    )
    if not joined:
        return
    uid = new.user.id
    uname = new.user.username or new.user.first_name

    inviter = None
    # 1) Native named invite link attribution (preferred)
    if res.invite_link and res.invite_link.invite_link:
        conn = db()
        row = conn.execute(
            "SELECT inviter_id FROM invite_links WHERE invite_link=?",
            (res.invite_link.invite_link,),
        ).fetchone()
        conn.close()
        if row:
            inviter = row["inviter_id"]
    # 2) Fallback: pending start-link invite
    if inviter is None:
        conn = db()
        p = conn.execute(
            "SELECT inviter_id FROM pending_invites WHERE invitee_id=?", (uid,)
        ).fetchone()
        conn.close()
        if p:
            inviter = p["inviter_id"]

    # Don't credit self-invites
    if inviter == uid:
        inviter = None
    ensure_user(uid, uname, invited_by=inviter)

# ----------------------------------------------------------------------------
# Automatic: message counting (spam exclusion handled with Rose/Telegram)
# ----------------------------------------------------------------------------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    # Talk points only count in the designated group (silent elsewhere)
    if not in_scoring_group(update):
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
    # invited user must have checked in at least once (last_checkin not null)
    rows = c.execute(
        "SELECT user_id, invited_by, joined_at, last_checkin, invite_credited "
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
        if hours >= INVITE_HOLD_HOURS and r["last_checkin"]:
            c.execute("UPDATE users SET invite_credited=1 WHERE user_id=?", (r["user_id"],))
            conn.commit()
            add_points(r["invited_by"], PTS_INVITE)
            credited += 1
    conn.close()
    if credited:
        log.info(f"Invite credit: awarded {credited} valid invite(s) this round")

async def job_close_polls(ctx: ContextTypes.DEFAULT_TYPE):
    """Every minute: close any poll whose deadline has passed so no one can
    vote after kickoff."""
    now = dt.datetime.utcnow()
    conn = db(); c = conn.cursor()
    rows = c.execute(
        "SELECT poll_id, chat_id, message_id, deadline FROM predict_polls "
        "WHERE deadline IS NOT NULL AND closed=0 AND settled=0"
    ).fetchall()
    conn.close()
    for r in rows:
        try:
            dl = dt.datetime.fromisoformat(r["deadline"])
        except Exception:
            continue
        if now >= dl and r["chat_id"] and r["message_id"]:
            try:
                await ctx.bot.stop_poll(chat_id=r["chat_id"], message_id=r["message_id"])
            except Exception as e:
                log.warning(f"stop_poll failed for {r['poll_id']}: {e}")
            conn = db(); conn.execute(
                "UPDATE predict_polls SET closed=1 WHERE poll_id=?", (r["poll_id"],)
            ); conn.commit(); conn.close()
            log.info(f"Auto-closed poll {r['poll_id']} at deadline")

# ----------------------------------------------------------------------------
# Predictions
# ----------------------------------------------------------------------------
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/predict <stage> <match> [deadline] <opt1> <opt2> [opt3]
    deadline is optional, UTC, format YYYY-MM-DDTHH:MM (e.g. 2026-06-17T17:00).
    If given, the bot auto-closes the poll at that time so no one can vote after
    kickoff.
    e.g. /predict group ARGvsFRA 2026-06-17T17:00 ARG_win Draw FRA_win
         /predict group ARGvsFRA ARG_win Draw FRA_win   (no deadline)
    """
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split()
    if len(parts) < 5:
        await update.message.reply_text(
            "Usage: /predict <stage> <match> [deadline] <opt1> <opt2> [opt3]\n"
            "deadline optional, UTC: YYYY-MM-DDTHH:MM\n"
            "e.g. /predict group ARGvsFRA 2026-06-17T17:00 ARG_win Draw FRA_win"
        )
        return
    stage = parts[1]
    match = parts[2]
    rest = parts[3:]
    # Detect optional deadline as the first item of rest
    deadline = None
    deadline_dt = None
    def parse_deadline(s):
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%dT%H:%M"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
    maybe = parse_deadline(rest[0])
    if maybe is not None:
        deadline_dt = maybe
        deadline = deadline_dt.isoformat()
        options = rest[1:]
    else:
        options = rest
    if len(options) < 2:
        await update.message.reply_text("Need at least 2 options (2-way win or 3-way with draw).")
        return
    if len(options) > 10:
        await update.message.reply_text("Telegram polls allow at most 10 options.")
        return
    sent = await ctx.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=f"⚽ {match} - who will win? ({stage})",
        options=options,
        is_anonymous=False,          # MUST be non-anonymous to score!
        allows_multiple_answers=False,
    )
    conn = db(); conn.execute(
        "INSERT INTO predict_polls (poll_id, match, stage, options, day, deadline, chat_id, message_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (sent.poll.id, match, stage, "|".join(options), today_str(),
         deadline, update.effective_chat.id, sent.message_id),
    ); conn.commit(); conn.close()
    if deadline_dt:
        await update.message.reply_text(
            f"✅ Poll opened: {match}. Voting +{PTS_PREDICT_JOIN}, correct guess earns more.\n"
            f"⏰ Auto-closes at {deadline_dt.strftime('%Y-%m-%d %H:%M')} UTC."
        )
    else:
        await update.message.reply_text(
            f"✅ Poll opened: {match}. Voting +{PTS_PREDICT_JOIN}, correct guess earns more.\n"
            f"(No deadline set — close it manually or settle to lock it.)"
        )

async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    if not ans.option_ids:
        return
    poll_id = ans.poll_id
    user = ans.user
    conn = db(); c = conn.cursor()
    poll = c.execute(
        "SELECT poll_id, settled, closed, deadline FROM predict_polls WHERE poll_id=?", (poll_id,)
    ).fetchone()
    if not poll or poll["settled"] == 1:
        conn.close(); return
    # Reject if poll is closed or past its deadline (no points, no record)
    if poll["closed"] == 1:
        conn.close(); return
    if poll["deadline"]:
        try:
            if dt.datetime.utcnow() >= dt.datetime.fromisoformat(poll["deadline"]):
                conn.close(); return
        except Exception:
            pass
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
    # First try exact match, then fall back to normalized (emoji/space-insensitive)
    poll = c.execute(
        "SELECT poll_id, match, options, stage, settled FROM predict_polls WHERE match=? ORDER BY rowid DESC", (match,)
    ).fetchone()
    if not poll:
        # normalized fallback: scan open polls and compare normalized names
        nmatch = norm(match)
        candidates = c.execute(
            "SELECT poll_id, match, options, stage, settled FROM predict_polls WHERE settled=0 ORDER BY rowid DESC"
        ).fetchall()
        for cand in candidates:
            if norm(cand["match"]) == nmatch:
                poll = cand
                break
    if not poll:
        open_rows = c.execute(
            "SELECT match FROM predict_polls WHERE settled=0 ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if open_rows:
            names = "\n".join(f"• {r['match']}" for r in open_rows)
            await update.message.reply_text(
                f"No poll found for: {match}\n\n"
                f"Open polls you can settle (use the EXACT name):\n{names}\n\n"
                f"Tip: send /polls to see names and options."
            )
        else:
            await update.message.reply_text(
                f"No poll found for: {match}\nNo open polls right now. Send /polls to check."
            )
        return
    if poll["settled"] == 1:
        conn.close(); await update.message.reply_text("This match has already been settled."); return
    options = poll["options"].split("|")
    # match the correct option with normalized comparison too
    correct_idx = None
    if correct in options:
        correct_idx = options.index(correct)
    else:
        ncorrect = norm(correct)
        for i, o in enumerate(options):
            if norm(o) == ncorrect:
                correct_idx = i
                break
    if correct_idx is None:
        conn.close()
        await update.message.reply_text(f"Invalid option. Choose from: {' / '.join(options)}"); return
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

async def cmd_reset_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """DANGER: wipe ALL data (users, points, predictions, invites) for a fresh
    start in a new group. Requires confirmation: /reset_all CONFIRM"""
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split()
    if len(parts) < 2 or parts[1].strip().upper() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ This will PERMANENTLY DELETE all users, points, predictions and "
            "invite links. This cannot be undone.\n\n"
            "If you are sure, send:\n/reset_all CONFIRM"
        )
        return
    conn = db(); c = conn.cursor()
    for tbl in ["users", "talk", "predict_polls", "predict_votes",
                "predict_streak", "pending_invites", "invite_links"]:
        c.execute(f"DELETE FROM {tbl}")
    conn.commit(); conn.close()
    await update.message.reply_text(
        "🧹 All data wiped. Fresh start! Everyone begins at 0 points.\n"
        "Make sure GROUP_CHAT_ID / GROUP_USERNAME now point to this new group."
    )

async def cmd_clearpolls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete ALL prediction polls + votes + streaks (test/junk cleanup).
    Does NOT touch user points already awarded. Requires: /clearpolls CONFIRM"""
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split()
    confirmed = len(parts) >= 2 and parts[1].strip().upper() == "CONFIRM"
    if not confirmed:
        await update.message.reply_text(
            "⚠️ This deletes ALL prediction polls and their vote records "
            "(useful to clear test/duplicate polls). Points already awarded stay.\n\n"
            "If sure, send:\n/clearpolls CONFIRM"
        )
        return
    conn = db(); c = conn.cursor()
    for tbl in ["predict_polls", "predict_votes", "predict_streak"]:
        c.execute(f"DELETE FROM {tbl}")
    conn.commit(); conn.close()
    await update.message.reply_text("🧹 All prediction polls cleared. You can open fresh polls now.")


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

async def cmd_polls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List recent prediction polls with their EXACT stored match names, so the
    admin can copy the correct name into /settle."""
    if not is_admin(update.effective_user.id):
        return
    conn = db()
    rows = conn.execute(
        "SELECT match, options, settled, day FROM predict_polls ORDER BY rowid DESC LIMIT 15"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No prediction polls found yet.")
        return
    lines = ["📋 Recent polls (copy the exact match name into /settle):\n"]
    for r in rows:
        status = "✅ settled" if r["settled"] else "🟡 open"
        opts = r["options"].replace("|", " / ")
        lines.append(f"{status}  |  match: {r['match']}\n   options: {opts}")
    lines.append("\nTo settle: /settle <match> <correct_option>")
    await update.message.reply_text("\n".join(lines))

# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable. Set it on your host.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start_with_payload))
    # /perpvia is the public-facing entry command (same welcome as /start).
    # /start stays registered because Telegram requires it for deep links
    # (invite tracking ?start=inv_xxx) and first-open of the bot.
    app.add_handler(CommandHandler("perpvia", cmd_start))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("invite", cmd_invite))

    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("settle", cmd_settle))
    app.add_handler(CommandHandler("award", cmd_award))
    app.add_handler(CommandHandler("top10", cmd_top10))
    app.add_handler(CommandHandler("reset_week", cmd_reset_week))
    app.add_handler(CommandHandler("reset_all", cmd_reset_all))
    app.add_handler(CommandHandler("clearpolls", cmd_clearpolls))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("count", cmd_count))
    app.add_handler(CommandHandler("polls", cmd_polls))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    app.add_handler(PollAnswerHandler(on_poll_answer))
    # Track joins for invite attribution
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    # Message counting (last, catches plain text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_repeating(job_credit_invites, interval=3600, first=60)
    app.job_queue.run_repeating(job_close_polls, interval=60, first=30)

    log.info("Perpvia bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
