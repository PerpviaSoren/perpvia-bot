# -*- coding: utf-8 -*-
"""
Perpvia Pioneer Points Race x World Cup Prediction Season - Telegram Points Bot
================================================================================
A ready-to-deploy points bot for Soren. All user-facing text is in English.

Scoring rules:
- Check-in /checkin        : +5, once a day; +20 bonus every 7-day streak
- Active chat >=5 msgs/day  : +5 (once per day max), spam not counted
- Invite (48h stay + checkin): +15 per valid user, no daily cap
- Prediction join           : +2/match; correct group stage +10 / knockout +8
- 3 correct in a row        : +15 bonus
- Screenshot/funnel tasks   : admin /award manual points
Dual-track:
- total_points  cumulative, never reset -> overall leaderboard
- week_points   weekly, reset every Monday via /reset_week
"""

import os
import logging
import datetime as dt
import sqlite3
import queue
import threading
import secrets

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    PollAnswerHandler, CallbackQueryHandler, ContextTypes, filters
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS   = set(int(x) for x in os.environ.get("ADMIN_IDS","").replace(" ","").split(",") if x)
GROUP_USERNAME = os.environ.get("GROUP_USERNAME","").lstrip("@")
GROUP_CHAT_ID  = int(os.environ.get("GROUP_CHAT_ID","0") or 0)
DB_PATH     = os.environ.get("DB_PATH","perpvia.db")

PTS_CHECKIN=5; PTS_CHECKIN_STREAK7=20; PTS_TALK_DAILY=5; TALK_THRESHOLD=5
PTS_INVITE=15; INVITE_HOLD_HOURS=48
PTS_PREDICT_JOIN=2; PTS_PREDICT_HIT_GROUP=10; PTS_PREDICT_HIT_KO=8; PTS_PREDICT_STREAK3=15

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("perpvia")

# ----------------------------------------------------------------------------
# Single-threaded database worker — ALL writes go through one thread.
# This is the correct way to use SQLite with concurrent Python async code.
# ----------------------------------------------------------------------------
_db_queue  = queue.Queue()
_db_conn   = None

def _db_worker():
    global _db_conn
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    while True:
        task = _db_queue.get()
        if task is None:
            break
        sql, params, result_q = task
        try:
            cur = _db_conn.execute(sql, params)
            _db_conn.commit()
            result_q.put(("ok", cur))
        except Exception as e:
            try: _db_conn.rollback()
            except: pass
            result_q.put(("err", e))

_db_thread = threading.Thread(target=_db_worker, daemon=True)
_db_thread.start()

def db_write(sql, params=()):
    """Execute a write statement on the DB thread. Returns cursor."""
    result_q = queue.Queue()
    _db_queue.put((sql, params, result_q))
    status, val = result_q.get(timeout=15)
    if status == "err":
        raise val
    return val

def db_read(sql, params=()):
    """Execute a read directly (reads are safe concurrent in WAL mode)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return rows
    finally:
        conn.close()

def db_read_one(sql, params=()):
    rows = db_read(sql, params)
    return rows[0] if rows else None

# ----------------------------------------------------------------------------
# DB init
# ----------------------------------------------------------------------------
def init_db():
    stmts = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            total_points INTEGER DEFAULT 0, week_points INTEGER DEFAULT 0,
            total_invites INTEGER DEFAULT 0, week_invites INTEGER DEFAULT 0,
            joined_at TEXT, invited_by INTEGER, invite_credited INTEGER DEFAULT 0,
            last_checkin TEXT, checkin_streak INTEGER DEFAULT 0, had_activity INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS talk (
            user_id INTEGER, day TEXT, count INTEGER DEFAULT 0, credited INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )""",
        """CREATE TABLE IF NOT EXISTS predict_polls (
            poll_id TEXT PRIMARY KEY, match TEXT, stage TEXT,
            options TEXT, settled INTEGER DEFAULT 0, day TEXT,
            deadline TEXT, chat_id INTEGER, message_id INTEGER, closed INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS predict_votes (
            poll_id TEXT, user_id INTEGER, choice INTEGER,
            PRIMARY KEY (poll_id, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS predict_streak (
            user_id INTEGER PRIMARY KEY, streak INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS pending_invites (
            invitee_id INTEGER PRIMARY KEY, inviter_id INTEGER, ts TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY, inviter_id INTEGER, created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS lotteries (
            lottery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            min_points INTEGER DEFAULT 0,
            max_winners INTEGER DEFAULT 1,
            point_type TEXT DEFAULT 'total',
            status TEXT DEFAULT 'open',
            created_at TEXT,
            drawn_at TEXT,
            chat_id INTEGER,
            message_id INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS lottery_entries (
            lottery_id INTEGER,
            user_id INTEGER,
            username TEXT,
            joined_at TEXT,
            PRIMARY KEY (lottery_id, user_id)
        )""",
    ]
    for s in stmts:
        db_write(s)
    # Safe migrations — predict_polls columns
    for col, decl in [("deadline","TEXT"),("chat_id","INTEGER"),
                      ("message_id","INTEGER"),("closed","INTEGER DEFAULT 0")]:
        try: db_write(f"ALTER TABLE predict_polls ADD COLUMN {col} {decl}")
        except: pass
    # Safe migrations — users invite-count columns
    # (FIX: these were previously ALTERed onto predict_polls by mistake,
    #  so old DBs were missing them on the users table -> /invitetop returned empty.)
    for col, decl in [("total_invites","INTEGER DEFAULT 0"),
                      ("week_invites","INTEGER DEFAULT 0")]:
        try: db_write(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        except: pass
    # Safe migrations — lotteries columns
    for col, decl in [("chat_id","INTEGER"), ("message_id","INTEGER")]:
        try: db_write(f"ALTER TABLE lotteries ADD COLUMN {col} {decl}")
        except: pass
    log.info("DB initialised")

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def today_str(): return dt.date.today().isoformat()

def is_admin(uid): return uid in ADMIN_IDS

def in_scoring_group(update):
    if not GROUP_CHAT_ID: return True
    chat = update.effective_chat
    return chat is not None and chat.id == GROUP_CHAT_ID

def norm(s):
    if not s: return ""
    return "".join(c.lower() for c in s if c.isalnum()
                   and not (0x1F1E6 <= ord(c) <= 0x1F1FF)
                   and not (0x1F000 <= ord(c) <= 0x1FAFF))

def ensure_user(user_id, username, invited_by=None):
    row = db_read_one("SELECT user_id, invited_by FROM users WHERE user_id=?", (user_id,))
    if not row:
        db_write("INSERT INTO users (user_id,username,joined_at,invited_by) VALUES (?,?,?,?)",
                 (user_id, username, dt.datetime.utcnow().isoformat(), invited_by))
    else:
        db_write("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        if invited_by and not row["invited_by"]:
            db_write("UPDATE users SET invited_by=? WHERE user_id=?", (invited_by, user_id))

def add_points(user_id, pts):
    db_write("UPDATE users SET total_points=total_points+?, week_points=week_points+? WHERE user_id=?",
             (pts, pts, user_id))

# ----------------------------------------------------------------------------
# User commands
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    group_line = (f"\n⚠️ Points are only earned inside our official group: https://t.me/{GROUP_USERNAME}\n"
                  if GROUP_USERNAME else "\n⚠️ Points are only earned inside our official community group.\n")
    await update.message.reply_text(
        "⚽ Welcome to the Perpvia Pioneer Points Race!\n\n"
        "Here in private chat you can check your progress:\n"
        "/me - View my points\n/rank - View my ranking\n/invite - Get my personal invite link\n"
        + group_line +
        "To EARN points (check-in, chat, predictions), do it in the group:\n"
        "/checkin - Daily check-in (+5, +20 bonus on a 7-day streak)\n\n"
        "Complete daily tasks, join the World Cup predictions, and climb the leaderboard!"
    )

async def cmd_start_with_payload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args; u = update.effective_user; invited_by = None
    if args and args[0].startswith("inv_"):
        try: invited_by = int(args[0][4:])
        except: pass
    if invited_by and invited_by != u.id:
        ensure_user(u.id, u.username or u.first_name, invited_by=invited_by)
        db_write("INSERT OR IGNORE INTO pending_invites (invitee_id,inviter_id,ts) VALUES (?,?,?)",
                 (u.id, invited_by, dt.datetime.utcnow().isoformat()))
        if GROUP_USERNAME:
            await update.message.reply_text(
                f"✅ You're in! Join the community: https://t.me/{GROUP_USERNAME}\n"
                "Then use /checkin to start earning points.")
            return
    await cmd_start(update, ctx)

async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not in_scoring_group(update):
        link = f"https://t.me/{GROUP_USERNAME}" if GROUP_USERNAME else "the Perpvia Pioneer Hub"
        await update.message.reply_text(f"📍 Check-in only counts in the official group.\n👉 {link}")
        return
    ensure_user(u.id, u.username or u.first_name)
    row = db_read_one("SELECT last_checkin, checkin_streak FROM users WHERE user_id=?", (u.id,))
    today = today_str()
    if row and row["last_checkin"] == today:
        await update.message.reply_text("You've already checked in today ✅ Come back tomorrow!")
        return
    yesterday = (dt.date.today()-dt.timedelta(days=1)).isoformat()
    streak = ((row["checkin_streak"] or 0)+1 if row and row["last_checkin"]==yesterday else 1)
    bonus = PTS_CHECKIN_STREAK7 if streak%7==0 else 0
    db_write("UPDATE users SET last_checkin=?,checkin_streak=?,had_activity=1 WHERE user_id=?",
             (today, streak, u.id))
    add_points(u.id, PTS_CHECKIN+bonus)
    msg = f"✅ Check-in successful! +{PTS_CHECKIN} points. Streak: {streak} day(s)."
    if bonus: msg += f"\n🎁 {streak}-day streak — extra +{bonus} points!"
    await update.message.reply_text(msg)

async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    row = db_read_one("SELECT total_points,week_points,checkin_streak FROM users WHERE user_id=?", (u.id,))
    await update.message.reply_text(
        f"📊 Your points\nOverall total: {row['total_points']} pts\n"
        f"This week: {row['week_points']} pts\nCheck-in streak: {row['checkin_streak']} day(s)")

async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = db_read("SELECT user_id FROM users ORDER BY total_points DESC")
    pos = next((i+1 for i,r in enumerate(rows) if r["user_id"]==u.id), None)
    if pos: await update.message.reply_text(f"🏆 Your overall rank: #{pos} of {len(rows)}")
    else: await update.message.reply_text("No points yet. Start with /checkin in the group!")

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    existing = db_read_one("SELECT invite_link FROM invite_links WHERE inviter_id=?", (u.id,))
    if existing:
        await update.message.reply_text(
            f"🔗 Your personal invite link:\n{existing['invite_link']}\n\n"
            f"Friends who join, stay {INVITE_HOLD_HOURS}h and check in earn you +{PTS_INVITE} pts each!")
        return
    if not GROUP_CHAT_ID:
        await update.message.reply_text("⚠️ Invite links not configured. Admin: set GROUP_CHAT_ID.")
        return
    try:
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            name=f"inv_{u.id}_{(u.username or u.first_name)}"[:32],
            creates_join_request=False)
    except Exception as e:
        log.error(f"create invite link: {e}")
        await update.message.reply_text("⚠️ Couldn't create link. Bot needs 'Invite Users via Link' admin permission.")
        return
    db_write("INSERT OR REPLACE INTO invite_links (invite_link,inviter_id,created_at) VALUES (?,?,?)",
             (link_obj.invite_link, u.id, dt.datetime.utcnow().isoformat()))
    await update.message.reply_text(
        f"🔗 Your personal invite link:\n{link_obj.invite_link}\n\n"
        f"Friends who join, stay {INVITE_HOLD_HOURS}h and check in earn you +{PTS_INVITE} pts each!")

async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(f"This chat's ID: {update.effective_chat.id}\nSet as GROUP_CHAT_ID env var.")

# ----------------------------------------------------------------------------
# Message counting
# ----------------------------------------------------------------------------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user: return
    if not in_scoring_group(update): return
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    day = today_str()
    db_write("INSERT INTO talk (user_id,day,count) VALUES (?,?,1) "
             "ON CONFLICT(user_id,day) DO UPDATE SET count=count+1", (u.id, day))
    db_write("UPDATE users SET had_activity=1 WHERE user_id=?", (u.id,))
    row = db_read_one("SELECT count,credited FROM talk WHERE user_id=? AND day=?", (u.id, day))
    if row and row["count"]>=TALK_THRESHOLD and row["credited"]==0:
        db_write("UPDATE talk SET credited=1 WHERE user_id=? AND day=?", (u.id, day))
        add_points(u.id, PTS_TALK_DAILY)

# ----------------------------------------------------------------------------
# Chat member tracking
# ----------------------------------------------------------------------------
async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    res = update.chat_member
    if not res: return
    new=res.new_chat_member; old=res.old_chat_member
    if not (old.status in ("left","kicked") and new.status in ("member","administrator","creator")):
        return
    uid=new.user.id; uname=new.user.username or new.user.first_name
    inviter=None
    if res.invite_link and res.invite_link.invite_link:
        row=db_read_one("SELECT inviter_id FROM invite_links WHERE invite_link=?",
                        (res.invite_link.invite_link,))
        if row: inviter=row["inviter_id"]
    if inviter is None:
        row=db_read_one("SELECT inviter_id FROM pending_invites WHERE invitee_id=?",(uid,))
        if row: inviter=row["inviter_id"]
    if inviter==uid: inviter=None
    ensure_user(uid, uname, invited_by=inviter)

# ----------------------------------------------------------------------------
# Invite 48h + checkin validation
# ----------------------------------------------------------------------------
async def job_credit_invites(ctx: ContextTypes.DEFAULT_TYPE):
    rows = db_read("SELECT user_id,invited_by,joined_at,last_checkin,invite_credited "
                   "FROM users WHERE invited_by IS NOT NULL AND invite_credited=0")
    now = dt.datetime.utcnow(); credited=0
    for r in rows:
        try: joined=dt.datetime.fromisoformat(r["joined_at"])
        except: continue
        if (now-joined).total_seconds()/3600 >= INVITE_HOLD_HOURS and r["last_checkin"]:
            db_write("UPDATE users SET invite_credited=1 WHERE user_id=?",(r["user_id"],))
            add_points(r["invited_by"], PTS_INVITE)
            db_write("UPDATE users SET total_invites=total_invites+1, week_invites=week_invites+1 WHERE user_id=?",
                     (r["invited_by"],))
            credited+=1
    if credited: log.info(f"Invite credit: {credited} valid invite(s)")

# ----------------------------------------------------------------------------
# Predictions
# ----------------------------------------------------------------------------
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/predict <stage> <match> [deadline YYYY-MM-DDTHH:MM UTC] <opt1> <opt2> [opt3]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split()
    if len(parts)<5:
        await update.message.reply_text(
            "Usage: /predict <stage> <match> [deadline] <opt1> <opt2> [opt3]\n"
            "e.g. /predict group ARGvsFRA 2026-06-17T17:00 ARG_win Draw FRA_win"); return
    stage=parts[1]; match_name=parts[2]; rest=parts[3:]
    deadline=None; deadline_dt=None
    def parse_dl(s):
        for fmt in ("%Y-%m-%dT%H:%M","%Y-%m-%d %H:%M"):
            try: return dt.datetime.strptime(s,fmt)
            except: pass
        return None
    if rest and parse_dl(rest[0]):
        deadline_dt=parse_dl(rest[0]); deadline=deadline_dt.isoformat(); options=rest[1:]
    else:
        options=rest
    if len(options)<2:
        await update.message.reply_text("Need at least 2 options."); return
    if len(options)>10:
        await update.message.reply_text("Max 10 options."); return
    sent=await ctx.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=f"⚽ {match_name} - who will win? ({stage})",
        options=options, is_anonymous=False, allows_multiple_answers=False)
    db_write("INSERT OR REPLACE INTO predict_polls "
             "(poll_id,match,stage,options,day,deadline,chat_id,message_id) VALUES (?,?,?,?,?,?,?,?)",
             (sent.poll.id, match_name, stage, "|".join(options), today_str(),
              deadline, update.effective_chat.id, sent.message_id))
    msg = f"✅ Poll opened: {match_name}. Voting +{PTS_PREDICT_JOIN}, correct guess earns more."
    if deadline_dt: msg += f"\n⏰ Auto-closes at {deadline_dt.strftime('%Y-%m-%d %H:%M')} UTC."
    await update.message.reply_text(msg)

async def job_close_polls(ctx: ContextTypes.DEFAULT_TYPE):
    rows=db_read("SELECT poll_id,chat_id,message_id,deadline FROM predict_polls "
                 "WHERE deadline IS NOT NULL AND closed=0 AND settled=0")
    now=dt.datetime.utcnow()
    for r in rows:
        try: dl=dt.datetime.fromisoformat(r["deadline"])
        except: continue
        if now>=dl and r["chat_id"] and r["message_id"]:
            try: await ctx.bot.stop_poll(chat_id=r["chat_id"],message_id=r["message_id"])
            except Exception as e: log.warning(f"stop_poll: {e}")
            db_write("UPDATE predict_polls SET closed=1 WHERE poll_id=?",(r["poll_id"],))
            log.info(f"Auto-closed poll {r['poll_id']}")

async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ans=update.poll_answer
    if not ans.option_ids: return
    poll_id=ans.poll_id; user=ans.user
    poll=db_read_one("SELECT poll_id,settled,closed,deadline FROM predict_polls WHERE poll_id=?",(poll_id,))
    if not poll or poll["settled"]==1 or poll["closed"]==1: return
    if poll["deadline"]:
        try:
            if dt.datetime.utcnow()>=dt.datetime.fromisoformat(poll["deadline"]): return
        except: pass
    ensure_user(user.id, user.username or user.first_name)
    exists=db_read_one("SELECT 1 FROM predict_votes WHERE poll_id=? AND user_id=?",(poll_id,user.id))
    db_write("INSERT OR REPLACE INTO predict_votes (poll_id,user_id,choice) VALUES (?,?,?)",
             (poll_id,user.id,ans.option_ids[0]))
    if not exists: add_points(user.id, PTS_PREDICT_JOIN)

async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settle <match> <correct_option>"""
    if not is_admin(update.effective_user.id): return
    parts=update.message.text.split(maxsplit=2)
    if len(parts)<3:
        await update.message.reply_text("Usage: /settle <match> <correct_option>"); return
    match_name,correct=parts[1],parts[2]
    poll=db_read_one("SELECT poll_id,match,options,stage,settled FROM predict_polls "
                     "WHERE match=? ORDER BY rowid DESC",(match_name,))
    if not poll:
        nm=norm(match_name)
        for cand in db_read("SELECT poll_id,match,options,stage,settled FROM predict_polls WHERE settled=0 ORDER BY rowid DESC"):
            if norm(cand["match"])==nm: poll=cand; break
    if not poll:
        open_rows=db_read("SELECT match FROM predict_polls WHERE settled=0 ORDER BY rowid DESC LIMIT 10")
        if open_rows:
            names="\n".join(f"• {r['match']}" for r in open_rows)
            await update.message.reply_text(f"No poll found for: {match_name}\n\nOpen polls:\n{names}\n\nTip: /polls")
        else:
            await update.message.reply_text(f"No poll found for: {match_name}. Send /polls to check.")
        return
    if poll["settled"]==1:
        await update.message.reply_text("Already settled."); return
    options=poll["options"].split("|")
    correct_idx=None
    if correct in options: correct_idx=options.index(correct)
    else:
        nc=norm(correct)
        for i,o in enumerate(options):
            if norm(o)==nc: correct_idx=i; break
    if correct_idx is None:
        await update.message.reply_text(f"Invalid option. Choose from: {' / '.join(options)}"); return
    is_ko="knockout" in poll["stage"].lower() or "ko" in poll["stage"].lower()
    hit_pts=PTS_PREDICT_HIT_KO if is_ko else PTS_PREDICT_HIT_GROUP
    votes=db_read("SELECT user_id,choice FROM predict_votes WHERE poll_id=?",(poll["poll_id"],))
    hit_users=[v["user_id"] for v in votes if v["choice"]==correct_idx]
    miss_users=[v["user_id"] for v in votes if v["choice"]!=correct_idx]
    streak_bonus=0
    for uid in hit_users:
        add_points(uid, hit_pts)
        row=db_read_one("SELECT streak FROM predict_streak WHERE user_id=?",(uid,))
        s=(row["streak"] if row else 0)+1
        db_write("INSERT OR REPLACE INTO predict_streak (user_id,streak) VALUES (?,?)",(uid,s))
        if s%3==0: add_points(uid,PTS_PREDICT_STREAK3); streak_bonus+=1
    for uid in miss_users:
        db_write("INSERT OR REPLACE INTO predict_streak (user_id,streak) VALUES (?,0)",(uid,))
    db_write("UPDATE predict_polls SET settled=1 WHERE poll_id=?",(poll["poll_id"],))
    await update.message.reply_text(
        f"✅ {match_name} settled! Correct: {correct}\n"
        f"{len(hit_users)} correct, +{hit_pts} pts each.\n"
        f"{streak_bonus} hit a 3-in-a-row streak, +{PTS_PREDICT_STREAK3} bonus.")

async def cmd_polls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows=db_read("SELECT match,options,settled,day FROM predict_polls ORDER BY rowid DESC LIMIT 15")
    if not rows:
        await update.message.reply_text("No prediction polls found yet."); return
    lines=["📋 Recent polls (copy match name into /settle):\n"]
    for r in rows:
        status="✅ settled" if r["settled"] else "🟡 open"
        opts=r["options"].replace("|"," / ")
        lines.append(f"{status}  |  match: {r['match']}\n   options: {opts}")
    lines.append("\nTo settle: /settle <match> <correct_option>")
    await update.message.reply_text("\n".join(lines))

# ----------------------------------------------------------------------------
# Lottery - button-based draw system
# ----------------------------------------------------------------------------
def lottery_keyboard(lottery_id, status="open"):
    if status != "open":
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Join Lottery", callback_data=f"lottery_join:{lottery_id}")],
        [InlineKeyboardButton("🏆 Draw Winners (Admin)", callback_data=f"lottery_draw:{lottery_id}")],
    ])

def lottery_text(lottery_id):
    lot = db_read_one(
        "SELECT lottery_id,title,min_points,max_winners,point_type,status FROM lotteries WHERE lottery_id=?",
        (lottery_id,)
    )
    if not lot:
        return "Lottery not found."
    count_row = db_read_one("SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=?", (lottery_id,))
    joined_count = count_row["n"] if count_row else 0
    point_label = "total points" if lot["point_type"] == "total" else "weekly points"
    status_label = "Open" if lot["status"] == "open" else lot["status"].capitalize()
    action_line = "Click the button below to join." if lot["status"] == "open" else "This lottery is closed."
    return (
        f"🎁 {lot['title']} Lottery\n\n"
        f"Status: {status_label}\n"
        f"Requirement: {lot['min_points']}+ {point_label}\n"
        f"Winners: {lot['max_winners']}\n"
        f"Participants: {joined_count}\n\n"
        f"{action_line}"
    )

async def refresh_lottery_message(ctx: ContextTypes.DEFAULT_TYPE, lottery_id):
    lot = db_read_one(
        "SELECT lottery_id,status,chat_id,message_id FROM lotteries WHERE lottery_id=?",
        (lottery_id,)
    )
    if not lot or not lot["chat_id"] or not lot["message_id"]:
        return
    try:
        await ctx.bot.edit_message_text(
            chat_id=lot["chat_id"],
            message_id=lot["message_id"],
            text=lottery_text(lottery_id),
            reply_markup=lottery_keyboard(lottery_id, lot["status"])
        )
    except Exception as e:
        # Usually caused by "message is not modified"; safe to ignore.
        log.debug(f"refresh lottery message: {e}")

async def draw_lottery(ctx: ContextTypes.DEFAULT_TYPE, lottery_id, reply_chat_id=None, source_message=None):
    lot = db_read_one(
        "SELECT lottery_id,title,min_points,max_winners,point_type,status,chat_id,message_id "
        "FROM lotteries WHERE lottery_id=?",
        (lottery_id,)
    )
    if not lot:
        return "Lottery not found."
    if lot["status"] != "open":
        return "This lottery has already been drawn or closed."

    rows = db_read(
        "SELECT e.user_id,e.username,u.total_points,u.week_points "
        "FROM lottery_entries e "
        "JOIN users u ON e.user_id=u.user_id "
        "WHERE e.lottery_id=?",
        (lottery_id,)
    )

    eligible = []
    for r in rows:
        pts = r["total_points"] if lot["point_type"] == "total" else r["week_points"]
        if pts >= lot["min_points"]:
            eligible.append(r)

    if not eligible:
        return "No eligible participants for this lottery."

    winner_count = min(lot["max_winners"], len(eligible))
    pool = list(eligible)
    winners = []
    for _ in range(winner_count):
        idx = secrets.randbelow(len(pool))
        winners.append(pool.pop(idx))

    db_write(
        "UPDATE lotteries SET status='drawn', drawn_at=? WHERE lottery_id=?",
        (dt.datetime.utcnow().isoformat(), lottery_id)
    )

    point_label = "total points" if lot["point_type"] == "total" else "weekly points"
    lines = [
        f"🎉 Lottery Result",
        f"Prize: {lot['title']}",
        f"Requirement: {lot['min_points']}+ {point_label}",
        f"Eligible participants: {len(eligible)}",
        "",
        "🏆 Winner(s):"
    ]
    for i, w in enumerate(winners, start=1):
        name = f"@{w['username']}" if w["username"] else f"User {w['user_id']}"
        pts = w["total_points"] if lot["point_type"] == "total" else w["week_points"]
        lines.append(f"{i}. {name} - {pts} pts")
    result_text = "\n".join(lines)

    await refresh_lottery_message(ctx, lottery_id)
    target_chat_id = reply_chat_id or lot["chat_id"]
    if target_chat_id:
        await ctx.bot.send_message(chat_id=target_chat_id, text=result_text)
    elif source_message:
        await source_message.reply_text(result_text)
    return None

async def cmd_lottery_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/lottery_create <min_points> <winners> <title/prize>"""
    if not is_admin(update.effective_user.id):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /lottery_create <min_points> <winners> <title>\n"
            "Example: /lottery_create 100 3 Perpvia Mystery Box"
        )
        return

    try:
        min_points = int(parts[1])
        max_winners = int(parts[2])
    except Exception:
        await update.message.reply_text("min_points and winners must be numbers.")
        return

    if min_points < 0:
        await update.message.reply_text("min_points cannot be negative.")
        return
    if max_winners <= 0:
        await update.message.reply_text("winners must be greater than 0.")
        return

    title = parts[3].strip()
    if not title:
        await update.message.reply_text("Lottery title cannot be empty.")
        return

    cur = db_write(
        "INSERT INTO lotteries (title,min_points,max_winners,point_type,status,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (title, min_points, max_winners, "total", "open", dt.datetime.utcnow().isoformat())
    )
    lottery_id = cur.lastrowid

    target_chat_id = GROUP_CHAT_ID or update.effective_chat.id
    try:
        sent = await ctx.bot.send_message(
            chat_id=target_chat_id,
            text=lottery_text(lottery_id),
            reply_markup=lottery_keyboard(lottery_id)
        )
    except Exception as e:
        log.error(f"send lottery message: {e}")
        await update.message.reply_text(
            "Lottery was created, but the bot could not publish the lottery message. "
            "Check GROUP_CHAT_ID and bot permissions."
        )
        return

    db_write(
        "UPDATE lotteries SET chat_id=?, message_id=? WHERE lottery_id=?",
        (target_chat_id, sent.message_id, lottery_id)
    )

    if update.effective_chat.id != target_chat_id:
        await update.message.reply_text(f"✅ Lottery #{lottery_id} published to the official group.")

async def cmd_lotteries(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db_read(
        "SELECT lottery_id,title,min_points,max_winners,status FROM lotteries ORDER BY lottery_id DESC LIMIT 10"
    )
    if not rows:
        await update.message.reply_text("No lotteries found yet.")
        return
    lines = ["🎁 Recent Lotteries\n"]
    for r in rows:
        count_row = db_read_one("SELECT COUNT(*) AS n FROM lottery_entries WHERE lottery_id=?", (r["lottery_id"],))
        joined_count = count_row["n"] if count_row else 0
        lines.append(
            f"ID: {r['lottery_id']} | {r['status']}\n"
            f"Prize: {r['title']}\n"
            f"Requirement: {r['min_points']}+ total points | Winners: {r['max_winners']} | Participants: {joined_count}\n"
        )
    await update.message.reply_text("\n".join(lines))

async def cmd_lottery_draw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin fallback command: /lottery_draw <lottery_id>"""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /lottery_draw <lottery_id>")
        return
    try:
        lottery_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Lottery ID must be a number.")
        return
    err = await draw_lottery(ctx, lottery_id, source_message=update.message)
    if err:
        await update.message.reply_text(err)

async def cmd_lottery_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/lottery_cancel <lottery_id>"""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /lottery_cancel <lottery_id>")
        return
    try:
        lottery_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Lottery ID must be a number.")
        return
    lot = db_read_one("SELECT lottery_id,status FROM lotteries WHERE lottery_id=?", (lottery_id,))
    if not lot:
        await update.message.reply_text("Lottery not found.")
        return
    if lot["status"] != "open":
        await update.message.reply_text("Only open lotteries can be cancelled.")
        return
    db_write("UPDATE lotteries SET status='cancelled' WHERE lottery_id=?", (lottery_id,))
    await refresh_lottery_message(ctx, lottery_id)
    await update.message.reply_text(f"Lottery #{lottery_id} cancelled.")

async def on_lottery_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if data.startswith("lottery_join:"):
        try:
            lottery_id = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("Invalid lottery.", show_alert=True)
            return

        user = query.from_user
        ensure_user(user.id, user.username or user.first_name)
        lot = db_read_one(
            "SELECT lottery_id,title,min_points,point_type,status FROM lotteries WHERE lottery_id=?",
            (lottery_id,)
        )
        if not lot:
            await query.answer("Lottery not found.", show_alert=True)
            return
        if lot["status"] != "open":
            await query.answer("This lottery is already closed.", show_alert=True)
            return

        user_row = db_read_one("SELECT total_points,week_points FROM users WHERE user_id=?", (user.id,))
        user_points = user_row["total_points"] if lot["point_type"] == "total" else user_row["week_points"]
        if user_points < lot["min_points"]:
            await query.answer(
                f"You need {lot['min_points']} points. Your points: {user_points}.",
                show_alert=True
            )
            return

        exists = db_read_one(
            "SELECT 1 FROM lottery_entries WHERE lottery_id=? AND user_id=?",
            (lottery_id, user.id)
        )
        if exists:
            await query.answer("You've already joined this lottery ✅", show_alert=False)
            return

        db_write(
            "INSERT INTO lottery_entries (lottery_id,user_id,username,joined_at) VALUES (?,?,?,?)",
            (lottery_id, user.id, user.username or user.first_name, dt.datetime.utcnow().isoformat())
        )
        await query.answer("✅ You joined this lottery!", show_alert=False)
        await refresh_lottery_message(ctx, lottery_id)
        return

    if data.startswith("lottery_draw:"):
        try:
            lottery_id = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("Invalid lottery.", show_alert=True)
            return
        if not is_admin(query.from_user.id):
            await query.answer("Admin only.", show_alert=True)
            return
        err = await draw_lottery(ctx, lottery_id, source_message=query.message)
        if err:
            await query.answer(err, show_alert=True)
        else:
            await query.answer("Lottery drawn.", show_alert=False)
        return

# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def cmd_award(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    target_id=None; target_name=None; pts=None
    if update.message.reply_to_message:
        t=update.message.reply_to_message.from_user
        target_id,target_name=t.id,(t.username or t.first_name)
        try: pts=int(update.message.text.split()[1])
        except: await update.message.reply_text("Usage (reply): reply to message + /award 30"); return
    else:
        parts=update.message.text.split()
        if len(parts)<3:
            await update.message.reply_text("Usage: /award @username 30  OR  reply to message + /award 30"); return
        target_name=parts[1].lstrip("@")
        try: pts=int(parts[2])
        except: await update.message.reply_text("Points must be a number."); return
        row=db_read_one("SELECT user_id FROM users WHERE username=?",(target_name,))
        if not row:
            await update.message.reply_text(f"User @{target_name} not found."); return
        target_id=row["user_id"]
    add_points(target_id, pts)
    await update.message.reply_text(f"✅ Awarded {pts} points to @{target_name}.")

async def cmd_top10(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    arg=ctx.args[0] if ctx.args else "total"
    col="week_points" if arg=="week" else "total_points"
    title="Weekly Leaderboard" if arg=="week" else "Overall Leaderboard"
    rows=db_read(f"SELECT username,{col} AS p FROM users ORDER BY {col} DESC LIMIT 10")
    medals=["🥇","🥈","🥉"]+["▫️"]*7
    lines=[f"🏆 {title} - TOP 10\n"]
    for i,r in enumerate(rows):
        name=f"@{r['username']}" if r["username"] else "Anonymous"
        lines.append(f"{medals[i]} {name} - {r['p']} pts")
    await update.message.reply_text("\n".join(lines))

async def cmd_invitetop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/invitetop → total invite leaderboard
    /invitetop week → weekly invite leaderboard"""
    if not is_admin(update.effective_user.id): return
    arg = ctx.args[0] if ctx.args else "total"
    col = "week_invites" if arg == "week" else "total_invites"
    title = "Weekly Invite Leaderboard" if arg == "week" else "Overall Invite Leaderboard"
    rows = db_read(
        f"SELECT username, {col} AS n FROM users WHERE {col}>0 ORDER BY {col} DESC LIMIT 10"
    )
    if not rows:
        await update.message.reply_text(f"No invite data yet for {title}."); return
    medals = ["🥇","🥈","🥉"] + ["▫️"]*7
    lines = [f"🤝 {title} - TOP 10\n"]
    for i, r in enumerate(rows):
        name = f"@{r['username']}" if r["username"] else "Anonymous"
        lines.append(f"{medals[i]} {name} - {r['n']} invite(s)")
    await update.message.reply_text("\n".join(lines))


async def cmd_reset_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    db_write("UPDATE users SET week_points=0, week_invites=0")
    await update.message.reply_text("✅ Weekly points and invite counts reset. New week begins! (Overall totals unaffected.)")

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    import csv, io
    rows=db_read("SELECT user_id,username,total_points,week_points,joined_at FROM users ORDER BY total_points DESC")
    buf=io.StringIO(); w=csv.writer(buf)
    w.writerow(["user_id","username","total_points","week_points","joined_at"])
    for r in rows: w.writerow([r["user_id"],r["username"],r["total_points"],r["week_points"],r["joined_at"]])
    buf.seek(0)
    data=io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name=f"perpvia_points_{today_str()}.csv"
    await ctx.bot.send_document(chat_id=update.effective_chat.id,document=data)

async def cmd_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total=db_read_one("SELECT COUNT(*) AS n FROM users")["n"]
    active=db_read_one("SELECT COUNT(*) AS n FROM users WHERE total_points>0")["n"]
    await update.message.reply_text(f"👥 {total} registered, {active} with points.")

async def cmd_clearpolls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts)<2 or parts[1].strip().upper()!="CONFIRM":
        await update.message.reply_text(
            "⚠️ Deletes ALL prediction polls. Points already awarded stay.\nSend: /clearpolls CONFIRM"); return
    for t in ["predict_polls","predict_votes","predict_streak"]: db_write(f"DELETE FROM {t}")
    await update.message.reply_text("🧹 All prediction polls cleared.")

async def cmd_reset_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts)<2 or parts[1].strip().upper()!="CONFIRM":
        await update.message.reply_text(
            "⚠️ PERMANENTLY DELETES all data. Cannot be undone.\nSend: /reset_all CONFIRM"); return
    for t in ["users","talk","predict_polls","predict_votes","predict_streak","pending_invites","invite_links","lotteries","lottery_entries"]:
        db_write(f"DELETE FROM {t}")
    await update.message.reply_text("🧹 All data wiped. Fresh start!")

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN: raise SystemExit("Missing BOT_TOKEN")
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start_with_payload))
    app.add_handler(CommandHandler("perpvia",cmd_start))
    app.add_handler(CommandHandler("checkin",cmd_checkin))
    app.add_handler(CommandHandler("me",cmd_me))
    app.add_handler(CommandHandler("rank",cmd_rank))
    app.add_handler(CommandHandler("invite",cmd_invite))
    app.add_handler(CommandHandler("predict",cmd_predict))
    app.add_handler(CommandHandler("settle",cmd_settle))
    app.add_handler(CommandHandler("polls",cmd_polls))
    app.add_handler(CommandHandler("lottery_create",cmd_lottery_create))
    app.add_handler(CommandHandler("lotteries",cmd_lotteries))
    app.add_handler(CommandHandler("lottery_draw",cmd_lottery_draw))
    app.add_handler(CommandHandler("lottery_cancel",cmd_lottery_cancel))
    app.add_handler(CommandHandler("award",cmd_award))
    app.add_handler(CommandHandler("top10",cmd_top10))
    app.add_handler(CommandHandler("invitetop",cmd_invitetop))
    app.add_handler(CommandHandler("reset_week",cmd_reset_week))
    app.add_handler(CommandHandler("export",cmd_export))
    app.add_handler(CommandHandler("count",cmd_count))
    app.add_handler(CommandHandler("chatid",cmd_chatid))
    app.add_handler(CommandHandler("clearpolls",cmd_clearpolls))
    app.add_handler(CommandHandler("reset_all",cmd_reset_all))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CallbackQueryHandler(on_lottery_button, pattern=r"^lottery_(join|draw):"))
    app.add_handler(ChatMemberHandler(on_chat_member,ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(job_credit_invites,interval=3600,first=60)
    app.job_queue.run_repeating(job_close_polls,interval=60,first=30)
    log.info("Perpvia bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
