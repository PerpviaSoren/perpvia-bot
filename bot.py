# -*- coding: utf-8 -*-
"""
Perpvia Pioneer Points Program - NFT Whitelist Rewards Telegram Bot
================================================================================
A ready-to-deploy points + rewards bot for Soren. All user-facing text is in English.

Scoring rules:
- Check-in /checkin        : +5, once a day; +20 bonus every 7-day streak
- Active chat >=5 msgs/day  : +5 (once per day max), spam not counted
- Invite (48h stay + checkin): +15 per valid user, no daily cap
- Screenshot/funnel tasks   : admin /award manual points
Dual-track:
- total_points  cumulative, never reset -> overall leaderboard
- week_points   weekly, reset every Monday via /reset_week

NFT Whitelist shop:
- Users link a wallet with /bindwallet, then redeem points for WL spots via /redeem
- Admins stock the shop with /additem and review redemptions with /redeemlist,
  approving with /fulfill or refunding with /reject

Personal commands (/checkin /me /rank /invite /bindwallet /shop /redeem /myredeems)
work both in DM and in the group. When used inside the group, the command and the
bot's reply are auto-deleted after a short delay to keep the group chat quiet
(requires the bot to have "Delete Messages" admin rights in the group).
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
    CallbackQueryHandler, ContextTypes, filters
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS   = set(int(x) for x in os.environ.get("ADMIN_IDS","").replace(" ","").split(",") if x)
GROUP_USERNAME = os.environ.get("GROUP_USERNAME","PerpViaPioneerHub").lstrip("@")
GROUP_CHAT_ID  = int(os.environ.get("GROUP_CHAT_ID","0") or 0)
DB_PATH     = os.environ.get("DB_PATH","perpvia.db")
GROUP_MSG_TTL  = int(os.environ.get("GROUP_MSG_TTL_SECONDS","8"))
REDEEM_CONFIRM_TTL = int(os.environ.get("REDEEM_CONFIRM_TTL_SECONDS","300"))

PTS_CHECKIN=5; PTS_CHECKIN_STREAK7=20; PTS_TALK_DAILY=5; TALK_THRESHOLD=5
MIN_MSG_LETTERS = int(os.environ.get("MIN_MSG_LETTERS","10"))
PTS_INVITE=15; INVITE_HOLD_HOURS=48

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
            last_checkin TEXT, checkin_streak INTEGER DEFAULT 0, had_activity INTEGER DEFAULT 0,
            wallet_address TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS talk (
            user_id INTEGER, day TEXT, count INTEGER DEFAULT 0, credited INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
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
        """CREATE TABLE IF NOT EXISTS shop_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            points_cost INTEGER,
            point_type TEXT DEFAULT 'total',
            stock INTEGER,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS redemptions (
            redemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            user_id INTEGER,
            username TEXT,
            wallet_address TEXT,
            points_spent INTEGER,
            point_type TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            resolved_at TEXT,
            note TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS member_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            event TEXT,
            ts TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS message_log (
            day TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            category TEXT DEFAULT 'basic',
            points INTEGER,
            repeatable INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS task_submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            user_id INTEGER,
            username TEXT,
            proof TEXT,
            photo_file_id TEXT,
            status TEXT DEFAULT 'pending',
            points_awarded INTEGER,
            created_at TEXT,
            resolved_at TEXT,
            note TEXT
        )""",
    ]
    for s in stmts:
        db_write(s)
    # Safe migrations — users columns
    for col, decl in [("total_invites","INTEGER DEFAULT 0"),
                      ("week_invites","INTEGER DEFAULT 0"),
                      ("wallet_address","TEXT")]:
        try: db_write(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        except: pass
    # Safe migrations — lotteries columns
    for col, decl in [("chat_id","INTEGER"), ("message_id","INTEGER")]:
        try: db_write(f"ALTER TABLE lotteries ADD COLUMN {col} {decl}")
        except: pass
    # Safe migrations — task_submissions columns
    try: db_write("ALTER TABLE task_submissions ADD COLUMN photo_file_id TEXT")
    except: pass
    # Safe migrations — shop_items columns
    try: db_write("ALTER TABLE shop_items ADD COLUMN description TEXT")
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

async def is_group_member(bot, user_id):
    """Used to validate DM-issued personal commands without requiring the user to type in-group."""
    if not GROUP_CHAT_ID:
        return True
    try:
        member = await bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

async def can_checkin_here(update, ctx):
    chat = update.effective_chat
    if chat.type == "private":
        return await is_group_member(ctx.bot, update.effective_user.id)
    return in_scoring_group(update)

# ----------------------------------------------------------------------------
# Group-chat cleanup — personal commands work in DM or in-group, but when used
# in-group we delete both the command and the reply after a short delay so the
# main chat doesn't get cluttered with check-in/shop noise.
# ----------------------------------------------------------------------------
async def _cleanup_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id, message_ids = ctx.job.data
    for mid in message_ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

def schedule_cleanup(ctx, chat_id, message_ids):
    ctx.job_queue.run_once(_cleanup_job, GROUP_MSG_TTL, data=(chat_id, message_ids))

def schedule_cleanup_after(ctx, chat_id, message_ids, delay_seconds):
    ctx.job_queue.run_once(_cleanup_job, delay_seconds, data=(chat_id, message_ids))

async def reply_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text, **kwargs):
    chat = update.effective_chat
    sent = await update.message.reply_text(text, **kwargs)
    if chat.type != "private":
        schedule_cleanup(ctx, chat.id, [update.message.message_id, sent.message_id])
    return sent

# ----------------------------------------------------------------------------
# User commands
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    group_line = (f"\n⚠️ Points are only earned inside our official group: https://t.me/{GROUP_USERNAME}\n"
                  if GROUP_USERNAME else "\n⚠️ Points are only earned inside our official community group.\n")
    await update.message.reply_text(
        "🎨 Welcome to the Perpvia Pioneer Points Program!\n\n"
        "You can use these commands here in DM, or in the group:\n"
        "/checkin - Daily check-in (+5, +20 bonus on a 7-day streak)\n"
        "/me - View my points\n/rank - View my ranking\n/invite - Get my personal invite link\n"
        "/tasks - Browse bonus tasks\n/submittask - Submit proof for a task\n/mytasks - My task submissions\n"
        "/shop - Browse the NFT Whitelist shop\n/redeem - Redeem points for a reward\n"
        "/myredeems - My redemption history\n"
        + group_line +
        "Earn points by checking in daily, chatting actively, inviting friends, and completing tasks — "
        "then redeem them for NFT Whitelist spots in the shop!"
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
    if not await can_checkin_here(update, ctx):
        link = f"https://t.me/{GROUP_USERNAME}" if GROUP_USERNAME else "the official group"
        await reply_cleanup(update, ctx, f"📍 Join the group first, then run /checkin here in DM or in the group.\n👉 {link}")
        return
    ensure_user(u.id, u.username or u.first_name)
    row = db_read_one("SELECT last_checkin, checkin_streak FROM users WHERE user_id=?", (u.id,))
    today = today_str()
    if row and row["last_checkin"] == today:
        await reply_cleanup(update, ctx, "You've already checked in today ✅ Come back tomorrow!")
        return
    yesterday = (dt.date.today()-dt.timedelta(days=1)).isoformat()
    streak = ((row["checkin_streak"] or 0)+1 if row and row["last_checkin"]==yesterday else 1)
    bonus = PTS_CHECKIN_STREAK7 if streak%7==0 else 0
    db_write("UPDATE users SET last_checkin=?,checkin_streak=?,had_activity=1 WHERE user_id=?",
             (today, streak, u.id))
    add_points(u.id, PTS_CHECKIN+bonus)
    msg = f"✅ Check-in successful! +{PTS_CHECKIN} points. Streak: {streak} day(s)."
    if bonus: msg += f"\n🎁 {streak}-day streak — extra +{bonus} points!"
    await reply_cleanup(update, ctx, msg)

async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    row = db_read_one("SELECT total_points,week_points,checkin_streak FROM users WHERE user_id=?", (u.id,))
    await reply_cleanup(update, ctx,
        f"📊 Your points\nOverall total: {row['total_points']} pts\n"
        f"This week: {row['week_points']} pts\nCheck-in streak: {row['checkin_streak']} day(s)")

async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = db_read("SELECT user_id FROM users ORDER BY total_points DESC")
    pos = next((i+1 for i,r in enumerate(rows) if r["user_id"]==u.id), None)
    if pos: await reply_cleanup(update, ctx, f"🏆 Your overall rank: #{pos} of {len(rows)}")
    else: await reply_cleanup(update, ctx, "No points yet. Start with /checkin!")

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    existing = db_read_one("SELECT invite_link FROM invite_links WHERE inviter_id=?", (u.id,))
    if existing:
        await reply_cleanup(update, ctx,
            f"🔗 Your personal invite link:\n{existing['invite_link']}\n\n"
            f"Friends who join, stay {INVITE_HOLD_HOURS}h and check in earn you +{PTS_INVITE} pts each!")
        return
    if not GROUP_CHAT_ID:
        await reply_cleanup(update, ctx, "⚠️ Invite links not configured. Admin: set GROUP_CHAT_ID.")
        return
    try:
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            name=f"inv_{u.id}_{(u.username or u.first_name)}"[:32],
            creates_join_request=False)
    except Exception as e:
        log.error(f"create invite link: {e}")
        await reply_cleanup(update, ctx, "⚠️ Couldn't create link. Bot needs 'Invite Users via Link' admin permission.")
        return
    db_write("INSERT OR REPLACE INTO invite_links (invite_link,inviter_id,created_at) VALUES (?,?,?)",
             (link_obj.invite_link, u.id, dt.datetime.utcnow().isoformat()))
    await reply_cleanup(update, ctx,
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
    # Raw message volume for /stats monitoring — counts every message, regardless
    # of the points-eligibility letter filter below.
    db_write("INSERT INTO message_log (day,count) VALUES (?,1) "
             "ON CONFLICT(day) DO UPDATE SET count=count+1", (today_str(),))
    letter_count = sum(1 for c in (update.message.text or "") if c.isascii() and c.isalpha())
    if letter_count <= MIN_MSG_LETTERS: return
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
    uid=new.user.id; uname=new.user.username or new.user.first_name
    joined = old.status in ("left","kicked") and new.status in ("member","administrator","creator")
    left = old.status in ("member","administrator","creator") and new.status in ("left","kicked")

    if left:
        db_write("INSERT INTO member_events (user_id,username,event,ts) VALUES (?,?,?,?)",
                 (uid, uname, "leave", dt.datetime.utcnow().isoformat()))
        return
    if not joined:
        return

    db_write("INSERT INTO member_events (user_id,username,event,ts) VALUES (?,?,?,?)",
             (uid, uname, "join", dt.datetime.utcnow().isoformat()))
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
# Tasks — admin-configured basic/UGC tasks, submitted by users and reviewed
# by admins (points aren't awarded until an admin approves the submission).
# ----------------------------------------------------------------------------
async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db_read("SELECT task_id,title,description,category,points,repeatable FROM tasks "
                   "WHERE status='active' ORDER BY category ASC, points ASC")
    if not rows:
        await reply_cleanup(update, ctx, "📋 No tasks available right now. Check back soon!")
        return
    by_cat = {}
    for r in rows:
        by_cat.setdefault((r["category"] or "basic").upper(), []).append(r)
    lines = ["📋 Perpvia Tasks\n"]
    for cat, items in by_cat.items():
        lines.append(f"— {cat} —")
        for r in items:
            repeat_label = " (repeatable)" if r["repeatable"] else ""
            lines.append(f"#{r['task_id']} — {r['title']} (+{r['points']} pts){repeat_label}")
            if r["description"]:
                lines.append(f"   {r['description']}")
        lines.append("")
    lines.append("Submit with: /submittask <task_id> <proof link/description>\n"
                 "Or attach a photo: reply to your photo with /submittask <task_id>,\n"
                 "or send the photo with caption /submittask <task_id> <description>")
    await reply_cleanup(update, ctx, "\n".join(lines))

async def handle_task_submission(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                 task_id, proof_text, photo_file_id=None, cleanup_original=True):
    """cleanup_original=False when `update.message` IS the proof photo itself (Pattern B below) —
    we don't want the group auto-cleanup to delete a user's UGC submission photo, only our own reply."""
    u = update.effective_user
    chat = update.effective_chat

    async def respond(text):
        sent = await update.message.reply_text(text)
        if chat.type != "private":
            ids = [update.message.message_id, sent.message_id] if cleanup_original else [sent.message_id]
            schedule_cleanup(ctx, chat.id, ids)
        return sent

    if not proof_text and not photo_file_id:
        await respond("Please include a proof link/description, or attach a photo.")
        return
    task = db_read_one("SELECT task_id,title,points,repeatable,status FROM tasks WHERE task_id=?", (task_id,))
    if not task or task["status"] != "active":
        await respond("Task not found.")
        return
    if not task["repeatable"]:
        exists = db_read_one(
            "SELECT 1 FROM task_submissions WHERE task_id=? AND user_id=? AND status IN ('pending','approved')",
            (task_id, u.id))
        if exists:
            await respond("You've already submitted (or completed) this task.")
            return
    ensure_user(u.id, u.username or u.first_name)
    proof_display = proof_text or "(photo attached, no description)"
    cur = db_write(
        "INSERT INTO task_submissions (task_id,user_id,username,proof,photo_file_id,status,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, u.id, u.username or u.first_name, proof_display, photo_file_id, "pending",
         dt.datetime.utcnow().isoformat()))
    sub_id = cur.lastrowid
    await respond(f"✅ Submitted for review: {task['title']}\nYou'll be notified here once an admin reviews it.")
    await notify_admins_task_submission(ctx, sub_id, u, task, proof_display, photo_file_id)

async def cmd_submittask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/submittask <task_id> <proof link/description> — or reply to your own proof photo/message."""
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await reply_cleanup(update, ctx,
            "Usage: /submittask <task_id> <proof link or description>\n"
            "Or reply to your proof photo/message with /submittask <task_id>\n"
            "Or send a photo with caption: /submittask <task_id> <description>")
        return
    try:
        task_id = int(parts[1])
    except Exception:
        await reply_cleanup(update, ctx, "task_id must be a number.")
        return
    proof_text = parts[2].strip() if len(parts) > 2 else ""
    photo_file_id = None
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        if replied.photo:
            photo_file_id = replied.photo[-1].file_id
        replied_note = (replied.text or replied.caption or ("photo" if replied.photo else "media"))[:200]
        proof_text = (proof_text + " | " if proof_text else "") + f"Reply ref: {replied_note}"
    await handle_task_submission(update, ctx, task_id, proof_text, photo_file_id)

async def on_photo_submittask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles a photo sent with caption '/submittask <task_id> [description]'.
    Never schedules the photo itself for auto-cleanup — only our own replies."""
    msg = update.message
    if not msg or not msg.photo or not msg.caption:
        return
    chat = update.effective_chat

    async def respond_only(text):
        sent = await msg.reply_text(text)
        if chat.type != "private":
            schedule_cleanup(ctx, chat.id, [sent.message_id])

    caption = msg.caption.strip()
    if not caption.lower().startswith("/submittask"):
        return
    parts = caption.split(maxsplit=2)
    if len(parts) < 2:
        await respond_only("Usage: send a photo with caption: /submittask <task_id> <description>")
        return
    try:
        task_id = int(parts[1])
    except Exception:
        await respond_only("task_id must be a number.")
        return
    proof_text = parts[2].strip() if len(parts) > 2 else ""
    photo_file_id = msg.photo[-1].file_id
    await handle_task_submission(update, ctx, task_id, proof_text, photo_file_id, cleanup_original=False)

async def notify_admins_task_submission(ctx, sub_id, user, task, proof, photo_file_id=None):
    submitter = f"@{user.username}" if user.username else (user.first_name or f"User {user.id}")
    text = (
        f"📥 New task submission #{sub_id}\n\n"
        f"User: {submitter}\n"
        f"Task: {task['title']} (+{task['points']} pts)\n"
        f"Proof: {proof}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"taskreview_approve:{sub_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"taskreview_reject:{sub_id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id:
                await ctx.bot.send_photo(chat_id=admin_id, photo=photo_file_id, caption=text[:1024], reply_markup=kb)
            else:
                await ctx.bot.send_message(chat_id=admin_id, text=text, reply_markup=kb)
        except Exception as e:
            log.warning(f"notify admin {admin_id} of submission {sub_id}: {e}")

async def cmd_mytasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = db_read("SELECT s.submission_id,t.title,s.status,s.points_awarded "
                   "FROM task_submissions s LEFT JOIN tasks t ON s.task_id=t.task_id "
                   "WHERE s.user_id=? ORDER BY s.submission_id DESC LIMIT 10", (u.id,))
    if not rows:
        await reply_cleanup(update, ctx, "You haven't submitted any tasks yet. Check /tasks!")
        return
    status_label = {"pending":"⏳ Pending", "approved":"✅ Approved", "rejected":"❌ Rejected"}
    lines = ["📝 Your task submissions\n"]
    for r in rows:
        pts_note = f" (+{r['points_awarded']} pts)" if r["points_awarded"] else ""
        lines.append(f"#{r['submission_id']} — {r['title'] or 'Unknown task'}{pts_note}\n"
                     f"   {status_label.get(r['status'], r['status'])}")
    await reply_cleanup(update, ctx, "\n".join(lines))

async def cmd_addtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/addtask <points> <category> <repeatable:0/1> <title>[|description]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=4)
    if len(parts) < 5:
        await update.message.reply_text(
            "Usage: /addtask <points> <category> <repeatable:0/1> <title>[|description]\n"
            "Example: /addtask 20 basic 0 Follow us on X|Follow @PerpviaNFT and stay followed.")
        return
    try:
        points = int(parts[1])
        repeatable = int(parts[3])
    except Exception:
        await update.message.reply_text("points and repeatable must be numbers (repeatable: 0 or 1).")
        return
    if points <= 0:
        await update.message.reply_text("points must be positive.")
        return
    if repeatable not in (0, 1):
        await update.message.reply_text("repeatable must be 0 or 1.")
        return
    category = parts[2].strip().lower() or "basic"
    rest = parts[4].strip()
    if "|" in rest:
        title, description = rest.split("|", 1)
        title = title.strip(); description = description.strip()
    else:
        title, description = rest, ""
    if not title:
        await update.message.reply_text("Title cannot be empty.")
        return
    cur = db_write("INSERT INTO tasks (title,description,category,points,repeatable,status,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (title, description, category, points, repeatable, "active", dt.datetime.utcnow().isoformat()))
    await update.message.reply_text(f"✅ Added task #{cur.lastrowid}: {title} (+{points} pts, {category})")

async def cmd_removetask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/removetask <task_id>"""
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: /removetask <task_id>")
        return
    try:
        task_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("task_id must be a number.")
        return
    cur = db_write("UPDATE tasks SET status='inactive' WHERE task_id=?", (task_id,))
    if cur.rowcount == 0:
        await update.message.reply_text("Task not found.")
        return
    await update.message.reply_text(f"🗑 Task #{task_id} removed.")

async def cmd_tasksubmissions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = db_read("SELECT s.submission_id,s.username,s.proof,s.photo_file_id,t.title,t.points "
                   "FROM task_submissions s LEFT JOIN tasks t ON s.task_id=t.task_id "
                   "WHERE s.status='pending' ORDER BY s.submission_id ASC LIMIT 20")
    if not rows:
        await update.message.reply_text("No pending task submissions.")
        return
    lines = ["📋 Pending task submissions\n"]
    for r in rows:
        name = f"@{r['username']}" if r["username"] else "Anonymous"
        photo_note = " 📷" if r["photo_file_id"] else ""
        lines.append(f"#{r['submission_id']} — {name}{photo_note}\n   Task: {r['title']} (+{r['points']} pts)\n   Proof: {r['proof']}\n")
    lines.append("Use /approvetask <id> or /rejecttask <id> [reason] to resolve.")
    await update.message.reply_text("\n".join(lines))

async def cmd_approvetask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/approvetask <submission_id> [points_override]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /approvetask <submission_id> [points_override]")
        return
    try:
        sid = int(parts[1])
    except Exception:
        await update.message.reply_text("submission_id must be a number.")
        return
    override = None
    if len(parts) > 2:
        try:
            override = int(parts[2])
        except Exception:
            await update.message.reply_text("points_override must be a number.")
            return
    row = db_read_one("SELECT s.submission_id,s.user_id,s.status,t.points,t.title FROM task_submissions s "
                      "LEFT JOIN tasks t ON s.task_id=t.task_id WHERE s.submission_id=?", (sid,))
    if not row:
        await update.message.reply_text("Submission not found.")
        return
    if row["status"] != "pending":
        await update.message.reply_text(f"Already {row['status']}.")
        return
    award_pts = override if override is not None else row["points"]
    add_points(row["user_id"], award_pts)
    db_write("UPDATE task_submissions SET status='approved', points_awarded=?, resolved_at=? WHERE submission_id=?",
             (award_pts, dt.datetime.utcnow().isoformat(), sid))
    await update.message.reply_text(f"✅ Submission #{sid} approved. +{award_pts} pts awarded.")
    try:
        await ctx.bot.send_message(chat_id=row["user_id"],
                                   text=f"🎉 Your task \"{row['title']}\" was approved! +{award_pts} points.")
    except Exception as e:
        log.warning(f"notify approvetask: {e}")

async def cmd_rejecttask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/rejecttask <submission_id> [reason]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /rejecttask <submission_id> [reason]")
        return
    try:
        sid = int(parts[1])
    except Exception:
        await update.message.reply_text("submission_id must be a number.")
        return
    reason = parts[2] if len(parts) > 2 else ""
    row = db_read_one("SELECT submission_id,user_id,status FROM task_submissions WHERE submission_id=?", (sid,))
    if not row:
        await update.message.reply_text("Submission not found.")
        return
    if row["status"] != "pending":
        await update.message.reply_text(f"Already {row['status']}.")
        return
    db_write("UPDATE task_submissions SET status='rejected', resolved_at=?, note=? WHERE submission_id=?",
             (dt.datetime.utcnow().isoformat(), reason, sid))
    await update.message.reply_text(f"❌ Submission #{sid} rejected.")
    try:
        note_line = f"\nReason: {reason}" if reason else ""
        await ctx.bot.send_message(chat_id=row["user_id"], text=f"Your task submission #{sid} was rejected.{note_line}")
    except Exception as e:
        log.warning(f"notify rejecttask: {e}")

async def on_task_review_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the Approve/Reject buttons pushed to admins by notify_admins_task_submission."""
    query = update.callback_query
    if not query or not query.data:
        return
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    try:
        action, sid_s = query.data.split(":")
        sid = int(sid_s)
    except Exception:
        await query.answer("Invalid submission.", show_alert=True)
        return

    row = db_read_one("SELECT s.submission_id,s.user_id,s.status,t.points,t.title FROM task_submissions s "
                      "LEFT JOIN tasks t ON s.task_id=t.task_id WHERE s.submission_id=?", (sid,))
    if not row:
        await query.answer("Submission not found.", show_alert=True)
        return

    reviewer = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name

    async def append_to_review_card(suffix):
        # The card may have been sent as a photo (with caption) or a plain text message.
        if query.message.photo:
            new_caption = (query.message.caption or "") + suffix
            await query.edit_message_caption(caption=new_caption[:1024])
        else:
            await query.edit_message_text((query.message.text or "") + suffix)

    if action == "taskreview_approve":
        award_pts = row["points"]
        cur = db_write(
            "UPDATE task_submissions SET status='approved', points_awarded=?, resolved_at=? "
            "WHERE submission_id=? AND status='pending'",
            (award_pts, dt.datetime.utcnow().isoformat(), sid))
        if cur.rowcount == 0:
            await query.answer(f"Already {row['status']}.", show_alert=True)
            return
        add_points(row["user_id"], award_pts)
        await query.answer("Approved.")
        await append_to_review_card(f"\n\n✅ Approved by {reviewer} (+{award_pts} pts)")
        try:
            await ctx.bot.send_message(chat_id=row["user_id"],
                                       text=f"🎉 Your task \"{row['title']}\" was approved! +{award_pts} points.")
        except Exception as e:
            log.warning(f"notify approvetask: {e}")
    else:
        cur = db_write(
            "UPDATE task_submissions SET status='rejected', resolved_at=? WHERE submission_id=? AND status='pending'",
            (dt.datetime.utcnow().isoformat(), sid))
        if cur.rowcount == 0:
            await query.answer(f"Already {row['status']}.", show_alert=True)
            return
        await query.answer("Rejected.")
        await append_to_review_card(f"\n\n❌ Rejected by {reviewer}")
        try:
            await ctx.bot.send_message(chat_id=row["user_id"], text=f"Your task submission #{sid} was rejected.")
        except Exception as e:
            log.warning(f"notify rejecttask: {e}")

# ----------------------------------------------------------------------------
# NFT Whitelist shop
# ----------------------------------------------------------------------------
async def cmd_bindwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/bindwallet <address>"""
    u = update.effective_user
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await reply_cleanup(update, ctx, "Usage: /bindwallet <your wallet address>\ne.g. /bindwallet 0x1234...abcd")
        return
    address = parts[1].strip()
    if " " in address or len(address) < 10 or len(address) > 100:
        await reply_cleanup(update, ctx, "That doesn't look like a valid wallet address. Please double-check and try again.")
        return
    ensure_user(u.id, u.username or u.first_name)
    db_write("UPDATE users SET wallet_address=? WHERE user_id=?", (address, u.id))
    await reply_cleanup(update, ctx,
        f"✅ Wallet linked:\n{address}\n\nThis address will be used for any NFT Whitelist rewards you redeem.")

async def cmd_shop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db_read("SELECT item_id,title,description,points_cost,point_type,stock FROM shop_items "
                   "WHERE status='active' ORDER BY points_cost ASC")
    if not rows:
        await reply_cleanup(update, ctx, "🛍 The shop is empty right now. Check back soon!")
        return
    lines = ["🛍 NFT Whitelist Shop\n"]
    for r in rows:
        label = "total points" if r["point_type"]=="total" else "weekly points"
        stock_label = f"{r['stock']} left" if r["stock"]>0 else "SOLD OUT"
        lines.append(f"#{r['item_id']} — {r['title']}\n   Cost: {r['points_cost']} {label} | {stock_label}")
        if r["description"]:
            lines.append(f"   {r['description']}")
    lines.append("\nRedeem with: /redeem <item_id>")
    await reply_cleanup(update, ctx, "\n".join(lines))

async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/redeem <item_id> — shows a confirmation prompt; the actual deduction happens
    in on_redeem_button once the user taps Confirm."""
    u = update.effective_user
    parts = update.message.text.split()
    if len(parts) < 2:
        await reply_cleanup(update, ctx, "Usage: /redeem <item_id>\nSee /shop for available items.")
        return
    try:
        item_id = int(parts[1])
    except Exception:
        await reply_cleanup(update, ctx, "Item ID must be a number.")
        return
    item = db_read_one(
        "SELECT item_id,title,description,points_cost,point_type,stock,status FROM shop_items WHERE item_id=?",
        (item_id,))
    if not item or item["status"] != "active":
        await reply_cleanup(update, ctx, "Item not found.")
        return
    if item["stock"] <= 0:
        await reply_cleanup(update, ctx, "This item is sold out.")
        return
    ensure_user(u.id, u.username or u.first_name)
    user_row = db_read_one("SELECT total_points,week_points,wallet_address FROM users WHERE user_id=?", (u.id,))
    col = "total_points" if item["point_type"]=="total" else "week_points"
    user_pts = user_row[col]
    if user_pts < item["points_cost"]:
        await reply_cleanup(update, ctx, f"Not enough points. You need {item['points_cost']}, you have {user_pts}.")
        return

    label = "total points" if item["point_type"]=="total" else "weekly points"
    desc_line = f"{item['description']}\n" if item["description"] else ""
    wallet_line = f"Wallet on file: {user_row['wallet_address']}\n" if user_row["wallet_address"] else ""
    cmd_msg_id = update.message.message_id
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"redeem_confirm:{item_id}:{u.id}:{cmd_msg_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"redeem_cancel:{item_id}:{u.id}:{cmd_msg_id}"),
    ]])
    sent = await update.message.reply_text(
        f"⚠️ Confirm redemption?\n\n"
        f"Item: {item['title']}\n"
        f"{desc_line}"
        f"Cost: {item['points_cost']} {label}\n"
        f"{wallet_line}\n"
        f"This cannot be undone once confirmed.",
        reply_markup=kb)
    if update.effective_chat.type != "private":
        # Fallback cleanup in case the user never taps a button.
        schedule_cleanup_after(ctx, update.effective_chat.id, [cmd_msg_id, sent.message_id], REDEEM_CONFIRM_TTL)

async def on_redeem_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        action, item_id_s, owner_id_s, cmd_msg_id_s = query.data.split(":")
        item_id = int(item_id_s); owner_id = int(owner_id_s); cmd_msg_id = int(cmd_msg_id_s)
    except Exception:
        await query.answer("Invalid request.", show_alert=True)
        return
    if query.from_user.id != owner_id:
        await query.answer("This isn't your redemption request.", show_alert=True)
        return

    chat = query.message.chat
    if action == "redeem_cancel":
        await query.answer("Cancelled.")
        await query.edit_message_text("❌ Redemption cancelled. No points were deducted.")
        if chat.type != "private":
            schedule_cleanup(ctx, chat.id, [query.message.message_id, cmd_msg_id])
        return

    item = db_read_one("SELECT item_id,title,points_cost,point_type,stock,status FROM shop_items WHERE item_id=?",
                       (item_id,))
    if not item or item["status"] != "active" or item["stock"] <= 0:
        await query.answer("This item is no longer available.", show_alert=True)
        await query.edit_message_text("❌ This item is no longer available.")
        return
    user_row = db_read_one("SELECT total_points,week_points,wallet_address FROM users WHERE user_id=?", (owner_id,))
    if not user_row:
        await query.answer("User not found.", show_alert=True)
        await query.edit_message_text("❌ Something went wrong. Please try again.")
        return
    col = "total_points" if item["point_type"]=="total" else "week_points"
    user_pts = user_row[col]
    if user_pts < item["points_cost"]:
        await query.answer("Not enough points.", show_alert=True)
        await query.edit_message_text("❌ Not enough points anymore.")
        return

    stock_cur = db_write("UPDATE shop_items SET stock=stock-1 WHERE item_id=? AND stock>0", (item_id,))
    if stock_cur.rowcount == 0:
        await query.answer("This item just sold out.", show_alert=True)
        await query.edit_message_text("❌ This item just sold out.")
        return
    pts_cur = db_write(f"UPDATE users SET {col}={col}-? WHERE user_id=? AND {col}>=?",
                       (item["points_cost"], owner_id, item["points_cost"]))
    if pts_cur.rowcount == 0:
        db_write("UPDATE shop_items SET stock=stock+1 WHERE item_id=?", (item_id,))
        await query.answer("Not enough points anymore.", show_alert=True)
        await query.edit_message_text("❌ Not enough points anymore.")
        return
    username = query.from_user.username or query.from_user.first_name
    db_write("INSERT INTO redemptions (item_id,user_id,username,wallet_address,points_spent,point_type,status,created_at) "
             "VALUES (?,?,?,?,?,?,?,?)",
             (item_id, owner_id, username, user_row["wallet_address"],
              item["points_cost"], item["point_type"], "pending", dt.datetime.utcnow().isoformat()))
    await query.answer("✅ Redeemed!")
    await query.edit_message_text(
        f"✅ Redeemed: {item['title']}\n-{item['points_cost']} points\n\n"
        f"Your request is pending admin review. You'll be notified here once it's confirmed.")
    if chat.type != "private":
        schedule_cleanup(ctx, chat.id, [query.message.message_id, cmd_msg_id])

async def cmd_myredeems(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = db_read("SELECT r.redemption_id,s.title,r.points_spent,r.status "
                   "FROM redemptions r LEFT JOIN shop_items s ON r.item_id=s.item_id "
                   "WHERE r.user_id=? ORDER BY r.redemption_id DESC LIMIT 10", (u.id,))
    if not rows:
        await reply_cleanup(update, ctx, "You haven't redeemed anything yet. Check /shop!")
        return
    status_label = {"pending":"⏳ Pending", "fulfilled":"✅ Fulfilled", "rejected":"❌ Rejected"}
    lines = ["🎟 Your redemptions\n"]
    for r in rows:
        lines.append(f"#{r['redemption_id']} — {r['title'] or 'Unknown item'} (-{r['points_spent']} pts)\n"
                     f"   {status_label.get(r['status'], r['status'])}")
    await reply_cleanup(update, ctx, "\n".join(lines))

async def cmd_additem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/additem <points_cost> <stock> <title>[|description]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /additem <points_cost> <stock> <title>[|description]\n"
            "Example: /additem 1000 5 Genesis WL Spot|Guaranteed spots for verified core contributors.")
        return
    try:
        points_cost = int(parts[1]); stock = int(parts[2])
    except Exception:
        await update.message.reply_text("points_cost and stock must be numbers.")
        return
    if points_cost <= 0 or stock <= 0:
        await update.message.reply_text("points_cost and stock must be positive.")
        return
    rest = parts[3].strip()
    if "|" in rest:
        title, description = rest.split("|", 1)
        title = title.strip(); description = description.strip()
    else:
        title, description = rest, ""
    if not title:
        await update.message.reply_text("Title cannot be empty.")
        return
    cur = db_write("INSERT INTO shop_items (title,description,points_cost,point_type,stock,status,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (title, description, points_cost, "total", stock, "active", dt.datetime.utcnow().isoformat()))
    await update.message.reply_text(f"✅ Added shop item #{cur.lastrowid}: {title} — {points_cost} pts, stock {stock}")

async def cmd_removeitem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/removeitem <item_id>"""
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: /removeitem <item_id>")
        return
    try:
        item_id = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("item_id must be a number.")
        return
    cur = db_write("UPDATE shop_items SET status='inactive' WHERE item_id=?", (item_id,))
    if cur.rowcount == 0:
        await update.message.reply_text("Item not found.")
        return
    await update.message.reply_text(f"🗑 Item #{item_id} removed from shop.")

async def cmd_edititem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/edititem <item_id> <title>[|description] — updates an existing item in place
    (use this instead of /additem when you just need to fix the title/description)."""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /edititem <item_id> <title>[|description]\n"
            "Example: /edititem 1 Via Genesis Core|Guaranteed spots for verified core contributors.")
        return
    try:
        item_id = int(parts[1])
    except Exception:
        await update.message.reply_text("item_id must be a number.")
        return
    rest = parts[2].strip()
    if "|" in rest:
        title, description = rest.split("|", 1)
        title = title.strip(); description = description.strip()
    else:
        title, description = rest, ""
    if not title:
        await update.message.reply_text("Title cannot be empty.")
        return
    cur = db_write("UPDATE shop_items SET title=?, description=? WHERE item_id=?", (title, description, item_id))
    if cur.rowcount == 0:
        await update.message.reply_text("Item not found.")
        return
    await update.message.reply_text(f"✅ Item #{item_id} updated: {title}")

async def cmd_redeemlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = db_read("SELECT r.redemption_id,r.username,r.wallet_address,r.points_spent,s.title "
                   "FROM redemptions r LEFT JOIN shop_items s ON r.item_id=s.item_id "
                   "WHERE r.status='pending' ORDER BY r.redemption_id ASC LIMIT 20")
    if not rows:
        await update.message.reply_text("No pending redemptions.")
        return
    lines = ["📋 Pending redemptions\n"]
    for r in rows:
        name = f"@{r['username']}" if r["username"] else "Anonymous"
        lines.append(f"#{r['redemption_id']} — {name}\n   Item: {r['title']}\n   Wallet: {r['wallet_address']}\n")
    lines.append("Use /fulfill <id> or /reject <id> [reason] to resolve.")
    await update.message.reply_text("\n".join(lines))

async def cmd_fulfill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/fulfill <redemption_id>"""
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: /fulfill <redemption_id>")
        return
    try:
        rid = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("redemption_id must be a number.")
        return
    row = db_read_one("SELECT redemption_id,user_id,status FROM redemptions WHERE redemption_id=?", (rid,))
    if not row:
        await update.message.reply_text("Redemption not found.")
        return
    if row["status"] != "pending":
        await update.message.reply_text(f"Already {row['status']}.")
        return
    db_write("UPDATE redemptions SET status='fulfilled', resolved_at=? WHERE redemption_id=?",
             (dt.datetime.utcnow().isoformat(), rid))
    await update.message.reply_text(f"✅ Redemption #{rid} marked as fulfilled.")
    try:
        await ctx.bot.send_message(chat_id=row["user_id"],
                                   text=f"🎉 Your redemption #{rid} has been fulfilled! Check your wallet.")
    except Exception as e:
        log.warning(f"notify fulfill: {e}")

async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/reject <redemption_id> [reason]"""
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /reject <redemption_id> [reason]")
        return
    try:
        rid = int(parts[1])
    except Exception:
        await update.message.reply_text("redemption_id must be a number.")
        return
    reason = parts[2] if len(parts) > 2 else ""
    row = db_read_one("SELECT redemption_id,item_id,user_id,points_spent,point_type,status "
                      "FROM redemptions WHERE redemption_id=?", (rid,))
    if not row:
        await update.message.reply_text("Redemption not found.")
        return
    if row["status"] != "pending":
        await update.message.reply_text(f"Already {row['status']}.")
        return
    col = "total_points" if row["point_type"]=="total" else "week_points"
    db_write(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (row["points_spent"], row["user_id"]))
    db_write("UPDATE shop_items SET stock=stock+1 WHERE item_id=?", (row["item_id"],))
    db_write("UPDATE redemptions SET status='rejected', resolved_at=?, note=? WHERE redemption_id=?",
             (dt.datetime.utcnow().isoformat(), reason, rid))
    await update.message.reply_text(f"❌ Redemption #{rid} rejected. Points and stock refunded.")
    try:
        note_line = f"\nReason: {reason}" if reason else ""
        await ctx.bot.send_message(chat_id=row["user_id"],
                                   text=f"Your redemption #{rid} was rejected and your points have been refunded.{note_line}")
    except Exception as e:
        log.warning(f"notify reject: {e}")

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
    entries = db_read("SELECT user_id,username FROM lottery_entries WHERE lottery_id=? ORDER BY joined_at ASC",
                      (lottery_id,))
    joined_count = len(entries)
    point_label = "total points" if lot["point_type"] == "total" else "weekly points"
    status_label = "Open" if lot["status"] == "open" else lot["status"].capitalize()
    action_line = "Click the button below to join." if lot["status"] == "open" else "This lottery is closed."

    MAX_SHOWN = 40
    if entries:
        names = [f"{i}. @{e['username']}" if e["username"] else f"{i}. User {e['user_id']}"
                 for i, e in enumerate(entries[:MAX_SHOWN], start=1)]
        participants_block = "\n".join(names)
        if joined_count > MAX_SHOWN:
            participants_block += f"\n…and {joined_count - MAX_SHOWN} more"
    else:
        participants_block = "No one has joined yet."

    return (
        f"🎁 {lot['title']} Lottery\n\n"
        f"Status: {status_label}\n"
        f"Requirement: {lot['min_points']}+ {point_label}\n"
        f"Winners: {lot['max_winners']}\n\n"
        f"👥 Participants ({joined_count}):\n{participants_block}\n\n"
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

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    today = today_str()
    week_ago = (dt.date.today()-dt.timedelta(days=7)).isoformat()

    joins_today = db_read_one("SELECT COUNT(*) AS n FROM member_events WHERE event='join' AND ts>=?", (today,))["n"]
    leaves_today = db_read_one("SELECT COUNT(*) AS n FROM member_events WHERE event='leave' AND ts>=?", (today,))["n"]
    joins_week = db_read_one("SELECT COUNT(*) AS n FROM member_events WHERE event='join' AND ts>=?", (week_ago,))["n"]
    leaves_week = db_read_one("SELECT COUNT(*) AS n FROM member_events WHERE event='leave' AND ts>=?", (week_ago,))["n"]

    msg_today_row = db_read_one("SELECT count FROM message_log WHERE day=?", (today,))
    msg_today = msg_today_row["count"] if msg_today_row else 0
    msg_total = db_read_one("SELECT COALESCE(SUM(count),0) AS n FROM message_log")["n"]

    active_today = db_read_one("SELECT COUNT(*) AS n FROM talk WHERE day=? AND count>0", (today,))["n"]
    checkins_today = db_read_one("SELECT COUNT(*) AS n FROM users WHERE last_checkin=?", (today,))["n"]

    total_users = db_read_one("SELECT COUNT(*) AS n FROM users")["n"]
    users_with_points = db_read_one("SELECT COUNT(*) AS n FROM users WHERE total_points>0")["n"]

    member_count_line = ""
    if GROUP_CHAT_ID:
        try:
            live_count = await ctx.bot.get_chat_member_count(GROUP_CHAT_ID)
            member_count_line = f"👥 Current group members: {live_count}\n\n"
        except Exception as e:
            log.warning(f"get_chat_member_count: {e}")

    text = (
        f"📈 Group Stats\n\n"
        f"{member_count_line}"
        f"🚪 Joins today: {joins_today}  |  Leaves today: {leaves_today}  (net {joins_today-leaves_today:+d})\n"
        f"🚪 Joins last 7d: {joins_week}  |  Leaves last 7d: {leaves_week}  (net {joins_week-leaves_week:+d})\n\n"
        f"💬 Messages today: {msg_today}\n"
        f"💬 Messages all-time: {msg_total}\n"
        f"🗣 Active chatters today: {active_today}\n"
        f"✅ Check-ins today: {checkins_today}\n\n"
        f"👤 Registered users: {total_users}  |  With points: {users_with_points}"
    )
    await update.message.reply_text(text)

async def cmd_reset_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    parts=update.message.text.split()
    if len(parts)<2 or parts[1].strip().upper()!="CONFIRM":
        await update.message.reply_text(
            "⚠️ PERMANENTLY DELETES all data. Cannot be undone.\nSend: /reset_all CONFIRM"); return
    for t in ["users","talk","pending_invites","invite_links","lotteries","lottery_entries","shop_items","redemptions",
              "member_events","message_log","tasks","task_submissions"]:
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
    app.add_handler(CommandHandler("tasks",cmd_tasks))
    app.add_handler(CommandHandler("submittask",cmd_submittask))
    app.add_handler(CommandHandler("mytasks",cmd_mytasks))
    app.add_handler(CommandHandler("addtask",cmd_addtask))
    app.add_handler(CommandHandler("removetask",cmd_removetask))
    app.add_handler(CommandHandler("tasksubmissions",cmd_tasksubmissions))
    app.add_handler(CommandHandler("approvetask",cmd_approvetask))
    app.add_handler(CommandHandler("rejecttask",cmd_rejecttask))
    app.add_handler(CommandHandler("bindwallet",cmd_bindwallet))
    app.add_handler(CommandHandler("shop",cmd_shop))
    app.add_handler(CommandHandler("redeem",cmd_redeem))
    app.add_handler(CommandHandler("myredeems",cmd_myredeems))
    app.add_handler(CommandHandler("additem",cmd_additem))
    app.add_handler(CommandHandler("removeitem",cmd_removeitem))
    app.add_handler(CommandHandler("edititem",cmd_edititem))
    app.add_handler(CommandHandler("redeemlist",cmd_redeemlist))
    app.add_handler(CommandHandler("fulfill",cmd_fulfill))
    app.add_handler(CommandHandler("reject",cmd_reject))
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
    app.add_handler(CommandHandler("stats",cmd_stats))
    app.add_handler(CommandHandler("chatid",cmd_chatid))
    app.add_handler(CommandHandler("reset_all",cmd_reset_all))
    app.add_handler(CallbackQueryHandler(on_lottery_button, pattern=r"^lottery_(join|draw):"))
    app.add_handler(CallbackQueryHandler(on_redeem_button, pattern=r"^redeem_(confirm|cancel):"))
    app.add_handler(CallbackQueryHandler(on_task_review_button, pattern=r"^taskreview_(approve|reject):"))
    app.add_handler(ChatMemberHandler(on_chat_member,ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo_submittask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(job_credit_invites,interval=3600,first=60)
    log.info("Perpvia bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
