# -*- coding: utf-8 -*-
"""
PerpVia Community Points Telegram Bot
PRD V1.0 implementation

Core rules:
- Weekly UTC+8 cycles, default Monday 00:00 through Sunday 23:59.
- No check-in points.
- Valid group chat message: +2 points, daily cap 20 points.
- Valid invite: +10 points after invitee sends one valid message within 3 days,
  daily cap 50 invite points.
- Weekly reward pool: 300U. Users with 100+ weekly points and active, non-blocked
  group membership qualify for proportional pending-review rewards.
"""

import csv
import datetime as dt
import hashlib
import io
import logging
import math
import os
import re
import sqlite3
import threading
import uuid
from decimal import Decimal, ROUND_HALF_UP

from telegram import Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x
}
GROUP_USERNAME = os.environ.get("GROUP_USERNAME", "PerpViaPioneerHub").lstrip("@")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "perpvia.db")

UTC8 = dt.timezone(dt.timedelta(hours=8), name="UTC+8")

DEFAULT_SETTINGS = {
    "cycle_length_days": os.environ.get("CYCLE_LENGTH_DAYS", "7"),
    "cycle_anchor_date": os.environ.get("CYCLE_ANCHOR_DATE", "2026-08-17"),
    "cycle_start_hour": os.environ.get("CYCLE_START_HOUR", "0"),
    "cycle_start_minute": os.environ.get("CYCLE_START_MINUTE", "0"),
    "valid_message_min_chars": os.environ.get("VALID_MESSAGE_MIN_CHARS", "15"),
    "chat_points_per_message": os.environ.get("CHAT_POINTS_PER_MESSAGE", "2"),
    "daily_chat_points_cap": os.environ.get("DAILY_CHAT_POINTS_CAP", "20"),
    "invite_points": os.environ.get("INVITE_POINTS", "10"),
    "daily_invite_points_cap": os.environ.get("DAILY_INVITE_POINTS_CAP", "50"),
    "invite_valid_days": os.environ.get("INVITE_VALID_DAYS", "3"),
    "weekly_reward_threshold": os.environ.get("WEEKLY_REWARD_THRESHOLD", "100"),
    "weekly_reward_pool": os.environ.get("WEEKLY_REWARD_POOL", "300"),
    "min_seconds_between_valid_messages": os.environ.get(
        "MIN_SECONDS_BETWEEN_VALID_MESSAGES", "0"
    ),
    "duplicate_message_window_minutes": os.environ.get(
        "DUPLICATE_MESSAGE_WINDOW_MINUTES", "360"
    ),
    "sensitive_words": os.environ.get(
        "SENSITIVE_WORDS",
        "private key,seed phrase,airdrop scam,metamask support,广告,诈骗,博彩",
    ),
    "google_form_url": os.environ.get("GOOGLE_FORM_URL", ""),
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("perpvia")

_db_lock = threading.RLock()


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
class DbResult:
    def __init__(self, lastrowid=None, rowcount=0):
        self.lastrowid = lastrowid
        self.rowcount = rowcount


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_write(sql, params=()):
    with _db_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return DbResult(cur.lastrowid, cur.rowcount)
        finally:
            conn.close()


def db_many(sql, rows):
    with _db_lock:
        conn = _connect()
        try:
            cur = conn.executemany(sql, rows)
            conn.commit()
            return DbResult(cur.lastrowid, cur.rowcount)
        finally:
            conn.close()


def db_read(sql, params=()):
    with _db_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()


def db_read_one(sql, params=()):
    rows = db_read(sql, params)
    return rows[0] if rows else None


def try_add_column(table, column, declaration):
    try:
        db_write(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    except sqlite3.OperationalError:
        pass


def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_bot INTEGER DEFAULT 0,
            joined_at TEXT,
            last_joined_at TEXT,
            left_at TEXT,
            invited_by INTEGER,
            is_blocked INTEGER DEFAULT 0,
            personal_invite_link TEXT,
            created_at TEXT,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS cycles (
            cycle_id TEXT PRIMARY KEY,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            settled_at TEXT,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS point_events (
            event_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            cycle_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            points INTEGER NOT NULL,
            source_id TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS invites (
            invite_id TEXT PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            invitee_id INTEGER NOT NULL,
            invite_link TEXT,
            joined_at TEXT,
            first_valid_message_at TEXT,
            status TEXT DEFAULT 'pending',
            points_awarded INTEGER DEFAULT 0,
            awarded_points INTEGER DEFAULT 0,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS rewards (
            reward_id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            weekly_points INTEGER NOT NULL,
            reward_amount TEXT NOT NULL,
            status TEXT DEFAULT 'pending_review',
            published_at TEXT,
            created_at TEXT,
            UNIQUE(cycle_id, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS message_audit (
            audit_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            message_id INTEGER,
            user_id INTEGER,
            cycle_id TEXT,
            day TEXT,
            text_hash TEXT,
            valid INTEGER DEFAULT 0,
            points_awarded INTEGER DEFAULT 0,
            reason TEXT,
            created_at TEXT
        )""",
    ]
    for statement in statements:
        db_write(statement)

    for col, decl in [
        ("first_name", "TEXT"),
        ("last_name", "TEXT"),
        ("is_bot", "INTEGER DEFAULT 0"),
        ("last_joined_at", "TEXT"),
        ("left_at", "TEXT"),
        ("is_blocked", "INTEGER DEFAULT 0"),
        ("personal_invite_link", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ]:
        try_add_column("users", col, decl)
    try_add_column("invite_links", "active", "INTEGER DEFAULT 1")
    try_add_column("invites", "awarded_points", "INTEGER DEFAULT 0")
    try_add_column("invites", "note", "TEXT")
    try_add_column("rewards", "status", "TEXT DEFAULT 'pending_review'")
    try_add_column("rewards", "published_at", "TEXT")

    now = now_local().isoformat()
    for key, value in DEFAULT_SETTINGS.items():
        db_write(
            "INSERT OR IGNORE INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (key, str(value), now),
        )
    ensure_cycle(*current_cycle_bounds())
    log.info("DB initialised")


# ----------------------------------------------------------------------------
# Time, settings, and cycles
# ----------------------------------------------------------------------------
def now_local():
    return dt.datetime.now(UTC8).replace(microsecond=0)


def parse_local(iso_value):
    if not iso_value:
        return None
    value = dt.datetime.fromisoformat(iso_value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC8)
    return value.astimezone(UTC8)


def setting(key):
    row = db_read_one("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        return row["value"]
    return DEFAULT_SETTINGS[key]


def setting_int(key):
    return int(setting(key))


def setting_decimal(key):
    return Decimal(str(setting(key)))


def cycle_id_from_start(start_at):
    return "C" + start_at.strftime("%Y%m%d")


def current_cycle_bounds(at=None):
    at = at or now_local()
    length_days = setting_int("cycle_length_days")
    anchor_date = dt.date.fromisoformat(setting("cycle_anchor_date"))
    anchor = dt.datetime.combine(
        anchor_date,
        dt.time(
            hour=setting_int("cycle_start_hour"),
            minute=setting_int("cycle_start_minute"),
            tzinfo=UTC8,
        ),
    )
    seconds_per_cycle = length_days * 86400
    index = math.floor((at - anchor).total_seconds() / seconds_per_cycle)
    start_at = anchor + dt.timedelta(days=index * length_days)
    end_at = start_at + dt.timedelta(days=length_days) - dt.timedelta(seconds=1)
    return cycle_id_from_start(start_at), start_at, end_at


def ensure_cycle(cycle_id, start_at, end_at):
    db_write(
        "INSERT OR IGNORE INTO cycles (cycle_id,start_at,end_at,status,created_at) "
        "VALUES (?,?,?,?,?)",
        (cycle_id, start_at.isoformat(), end_at.isoformat(), "open", now_local().isoformat()),
    )


def current_cycle():
    cycle_id, start_at, end_at = current_cycle_bounds()
    ensure_cycle(cycle_id, start_at, end_at)
    return {"cycle_id": cycle_id, "start_at": start_at, "end_at": end_at}


def format_day(value):
    return value.strftime("%b ") + str(value.day)


def format_cycle(start_at, end_at):
    return f"{format_day(start_at)} - {format_day(end_at)} UTC+8"


def local_day(at=None):
    return (at or now_local()).date().isoformat()


# ----------------------------------------------------------------------------
# User and scoring helpers
# ----------------------------------------------------------------------------
def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_activity_group(update):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return False
    if GROUP_CHAT_ID:
        return chat.id == GROUP_CHAT_ID
    return True


def display_name(row):
    if row["username"]:
        return f"@{row['username']}"
    name = " ".join(x for x in [row["first_name"], row["last_name"]] if x)
    return name or f"User {row['user_id']}"


def upsert_user(tg_user, joined_at=None, invited_by=None):
    if not tg_user:
        return
    now = now_local().isoformat()
    joined_iso = joined_at.isoformat() if joined_at else None
    row = db_read_one("SELECT user_id, joined_at, invited_by FROM users WHERE user_id=?", (tg_user.id,))
    if not row:
        db_write(
            "INSERT INTO users "
            "(user_id,username,first_name,last_name,is_bot,joined_at,last_joined_at,"
            "invited_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tg_user.id,
                tg_user.username,
                tg_user.first_name,
                tg_user.last_name,
                int(tg_user.is_bot),
                joined_iso,
                joined_iso,
                invited_by,
                now,
                now,
            ),
        )
        return

    if joined_iso:
        db_write(
            "UPDATE users SET username=?, first_name=?, last_name=?, is_bot=?, "
            "last_joined_at=?, left_at=NULL, joined_at=COALESCE(joined_at, ?), "
            "updated_at=? WHERE user_id=?",
            (
                tg_user.username,
                tg_user.first_name,
                tg_user.last_name,
                int(tg_user.is_bot),
                joined_iso,
                joined_iso,
                now,
                tg_user.id,
            ),
        )
    else:
        db_write(
            "UPDATE users SET username=?, first_name=?, last_name=?, is_bot=?, "
            "updated_at=? WHERE user_id=?",
            (
                tg_user.username,
                tg_user.first_name,
                tg_user.last_name,
                int(tg_user.is_bot),
                now,
                tg_user.id,
            ),
        )
    if invited_by and not row["invited_by"]:
        db_write("UPDATE users SET invited_by=? WHERE user_id=?", (invited_by, tg_user.id))


def resolve_user(token):
    token = token.strip()
    if token.startswith("@"):
        username = token[1:]
        return db_read_one("SELECT * FROM users WHERE lower(username)=lower(?)", (username,))
    if token.isdigit():
        return db_read_one("SELECT * FROM users WHERE user_id=?", (int(token),))
    return db_read_one("SELECT * FROM users WHERE lower(username)=lower(?)", (token,))


def record_point_event(user_id, event_type, points, source_id, reason, at=None):
    at = at or now_local()
    cycle_id, start_at, end_at = current_cycle_bounds(at)
    ensure_cycle(cycle_id, start_at, end_at)
    event_id = uuid.uuid4().hex
    db_write(
        "INSERT INTO point_events "
        "(event_id,user_id,cycle_id,event_type,points,source_id,reason,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (event_id, user_id, cycle_id, event_type, points, source_id, reason, at.isoformat()),
    )
    return event_id


def points_sum(user_id, cycle_id, event_type=None, day=None):
    sql = "SELECT COALESCE(SUM(points),0) AS n FROM point_events WHERE user_id=? AND cycle_id=?"
    params = [user_id, cycle_id]
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    if day:
        sql += " AND substr(created_at,1,10)=?"
        params.append(day)
    return int(db_read_one(sql, tuple(params))["n"])


def cycle_total_points(cycle_id, user_id):
    return points_sum(user_id, cycle_id)


async def is_group_member(bot, user_id):
    if not GROUP_CHAT_ID:
        return True
    try:
        member = await bot.get_chat_member(GROUP_CHAT_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception as exc:
        log.warning("get_chat_member user=%s failed: %s", user_id, exc)
        return False


# ----------------------------------------------------------------------------
# Message validation and chat scoring
# ----------------------------------------------------------------------------
URL_ONLY_RE = re.compile(r"^((https?://|www\.|t\.me/)\S+\s*)+$", re.IGNORECASE)
URL_RE = re.compile(r"(https?://|www\.|t\.me/)", re.IGNORECASE)


def meaningful_len(text):
    return sum(1 for ch in text if not ch.isspace())


def has_letters_or_numbers(text):
    return any(ch.isalnum() for ch in text)


def text_hash(text):
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def repeated_char_spam(text):
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    if re.search(r"(.)\1{7,}", compact):
        return True
    if len(compact) >= 20 and len(set(compact)) <= 3:
        return True
    return False


def sensitive_hit(text):
    lowered = text.lower()
    words = [w.strip().lower() for w in setting("sensitive_words").split(",") if w.strip()]
    return next((word for word in words if word and word in lowered), None)


def validate_message(update):
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return False, "missing_message", ""
    if user.is_bot:
        return False, "bot_user", ""
    if not is_activity_group(update):
        return False, "outside_activity_group", ""
    if getattr(msg, "forward_origin", None) or getattr(msg, "forward_date", None):
        return False, "forwarded_message", ""
    text = msg.text or ""
    stripped = text.strip()
    if not stripped:
        return False, "non_text_or_empty", ""
    if meaningful_len(stripped) < setting_int("valid_message_min_chars"):
        return False, "too_short", text_hash(stripped)
    if URL_ONLY_RE.match(stripped):
        return False, "pure_link", text_hash(stripped)
    if not has_letters_or_numbers(stripped):
        return False, "no_meaningful_text", text_hash(stripped)
    bad_word = sensitive_hit(stripped)
    if bad_word:
        return False, f"sensitive:{bad_word}", text_hash(stripped)
    if repeated_char_spam(stripped):
        return False, "repeated_char_spam", text_hash(stripped)

    h = text_hash(stripped)
    duplicate_cutoff = now_local() - dt.timedelta(
        minutes=setting_int("duplicate_message_window_minutes")
    )
    duplicate = db_read_one(
        "SELECT audit_id FROM message_audit "
        "WHERE user_id=? AND text_hash=? AND valid=1 AND created_at>=? LIMIT 1",
        (user.id, h, duplicate_cutoff.isoformat()),
    )
    if duplicate:
        return False, "duplicate_message", h

    min_gap = setting_int("min_seconds_between_valid_messages")
    if min_gap > 0:
        last = db_read_one(
            "SELECT created_at FROM message_audit WHERE user_id=? AND valid=1 "
            "ORDER BY created_at DESC LIMIT 1",
            (user.id,),
        )
        if last:
            last_at = parse_local(last["created_at"])
            if last_at and (now_local() - last_at).total_seconds() < min_gap:
                return False, "message_interval_too_short", h
    return True, "valid", h


def log_message(update, cycle_id, day, text_hash_value, valid, points_awarded, reason):
    msg = update.message
    user = update.effective_user
    db_write(
        "INSERT INTO message_audit "
        "(audit_id,chat_id,message_id,user_id,cycle_id,day,text_hash,valid,"
        "points_awarded,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            update.effective_chat.id if update.effective_chat else None,
            msg.message_id if msg else None,
            user.id if user else None,
            cycle_id,
            day,
            text_hash_value,
            int(valid),
            points_awarded,
            reason,
            now_local().isoformat(),
        ),
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    upsert_user(update.effective_user)
    cycle = current_cycle()
    day = local_day()
    valid, reason, h = validate_message(update)
    if not valid:
        log_message(update, cycle["cycle_id"], day, h, False, 0, reason)
        return

    user = update.effective_user
    earned_today = points_sum(user.id, cycle["cycle_id"], "chat", day)
    cap = setting_int("daily_chat_points_cap")
    per_message = setting_int("chat_points_per_message")
    award = 0
    score_reason = "daily_chat_cap"
    if earned_today < cap:
        award = min(per_message, cap - earned_today)
        record_point_event(
            user.id,
            "chat",
            award,
            f"{update.effective_chat.id}:{update.message.message_id}",
            "valid chat message",
        )
        score_reason = "valid_chat_scored"

    log_message(update, cycle["cycle_id"], day, h, True, award, score_reason)
    await maybe_validate_invite(ctx, user.id)


# ----------------------------------------------------------------------------
# Invite tracking and scoring
# ----------------------------------------------------------------------------
async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    existing = db_read_one(
        "SELECT invite_link FROM invite_links WHERE inviter_id=? AND active=1",
        (user.id,),
    )
    if existing:
        await update.message.reply_text(
            "Your personal PerpVia invite link:\n"
            f"{existing['invite_link']}\n\n"
            "Invitees must join through this link and send one valid message within 3 days."
        )
        return

    if not GROUP_CHAT_ID:
        await update.message.reply_text(
            "Invite links are not configured yet. Admin must set GROUP_CHAT_ID."
        )
        return

    try:
        link_obj = await ctx.bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            name=f"perpvia_{user.id}"[:32],
            creates_join_request=False,
        )
    except Exception as exc:
        log.error("create_chat_invite_link failed: %s", exc)
        await update.message.reply_text(
            "Could not create the invite link. The bot needs invite-link admin permission."
        )
        return

    db_write(
        "INSERT OR REPLACE INTO invite_links (invite_link,inviter_id,created_at,active) "
        "VALUES (?,?,?,1)",
        (link_obj.invite_link, user.id, now_local().isoformat()),
    )
    db_write(
        "UPDATE users SET personal_invite_link=? WHERE user_id=?",
        (link_obj.invite_link, user.id),
    )
    await update.message.reply_text(
        "Your personal PerpVia invite link:\n"
        f"{link_obj.invite_link}\n\n"
        "You earn 10 Points when an invitee sends one valid message within 3 days. "
        "Daily invite points are capped at 50."
    )


async def on_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    change = update.chat_member
    if not change:
        return
    if GROUP_CHAT_ID and change.chat.id != GROUP_CHAT_ID:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    user = change.new_chat_member.user
    joined = old_status in ("left", "kicked") and new_status in (
        "member",
        "administrator",
        "creator",
    )
    left = old_status in ("member", "administrator", "creator") and new_status in (
        "left",
        "kicked",
    )

    if left:
        upsert_user(user)
        db_write(
            "UPDATE users SET left_at=?, updated_at=? WHERE user_id=?",
            (now_local().isoformat(), now_local().isoformat(), user.id),
        )
        return
    if not joined:
        return

    joined_at = now_local()
    inviter_id = None
    invite_link = None
    if change.invite_link and change.invite_link.invite_link:
        invite_link = change.invite_link.invite_link
        link_row = db_read_one(
            "SELECT inviter_id FROM invite_links WHERE invite_link=? AND active=1",
            (invite_link,),
        )
        if link_row:
            inviter_id = link_row["inviter_id"]
    if inviter_id == user.id:
        inviter_id = None

    upsert_user(user, joined_at=joined_at, invited_by=inviter_id)
    if not inviter_id:
        return

    previous = db_read_one(
        "SELECT invite_id,status FROM invites WHERE invitee_id=? ORDER BY created_at ASC LIMIT 1",
        (user.id,),
    )
    if previous:
        return

    db_write(
        "INSERT INTO invites "
        "(invite_id,inviter_id,invitee_id,invite_link,joined_at,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            inviter_id,
            user.id,
            invite_link,
            joined_at.isoformat(),
            "pending",
            now_local().isoformat(),
            now_local().isoformat(),
        ),
    )


async def maybe_validate_invite(ctx, invitee_id):
    invite = db_read_one(
        "SELECT * FROM invites WHERE invitee_id=? AND status='pending' ORDER BY joined_at ASC LIMIT 1",
        (invitee_id,),
    )
    if not invite:
        return

    now = now_local()
    joined_at = parse_local(invite["joined_at"])
    if not joined_at:
        return
    if now > joined_at + dt.timedelta(days=setting_int("invite_valid_days")):
        db_write(
            "UPDATE invites SET status='expired', note=?, updated_at=? WHERE invite_id=?",
            ("No valid message within invite validity window.", now.isoformat(), invite["invite_id"]),
        )
        return

    already_valid = db_read_one(
        "SELECT invite_id FROM invites WHERE invitee_id=? AND status='valid' LIMIT 1",
        (invitee_id,),
    )
    if already_valid:
        db_write(
            "UPDATE invites SET status='rejected', note=?, updated_at=? WHERE invite_id=?",
            ("Invitee already counted as valid before.", now.isoformat(), invite["invite_id"]),
        )
        return

    cycle = current_cycle()
    earned_today = points_sum(invite["inviter_id"], cycle["cycle_id"], "invite", local_day(now))
    invite_points = setting_int("invite_points")
    daily_cap = setting_int("daily_invite_points_cap")
    award = invite_points if earned_today + invite_points <= daily_cap else 0

    if award:
        record_point_event(
            invite["inviter_id"],
            "invite",
            award,
            invite["invite_id"],
            f"valid invite: {invitee_id}",
            now,
        )
    db_write(
        "UPDATE invites SET status='valid', first_valid_message_at=?, points_awarded=?, "
        "awarded_points=?, note=?, updated_at=? WHERE invite_id=?",
        (
            now.isoformat(),
            int(bool(award)),
            award,
            "Valid invite scored." if award else "Valid invite, daily invite cap reached.",
            now.isoformat(),
            invite["invite_id"],
        ),
    )


async def expire_pending_invites(ctx: ContextTypes.DEFAULT_TYPE):
    cutoff = now_local() - dt.timedelta(days=setting_int("invite_valid_days"))
    rows = db_read(
        "SELECT invite_id FROM invites WHERE status='pending' AND joined_at<?",
        (cutoff.isoformat(),),
    )
    if not rows:
        return
    db_many(
        "UPDATE invites SET status='expired', note=?, updated_at=? WHERE invite_id=?",
        [
            (
                "No valid message within invite validity window.",
                now_local().isoformat(),
                row["invite_id"],
            )
            for row in rows
        ],
    )
    log.info("Expired %s pending invite(s)", len(rows))


# ----------------------------------------------------------------------------
# User commands
# ----------------------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    await update.message.reply_text(
        "Welcome to PerpVia Community Points.\n\n"
        "Earn weekly points through valid community chat, valid invites, and occasional "
        "admin-scored raid tasks. Weekly qualified users share the 300U contract-trial "
        "reward pool proportionally.\n\n"
        "Commands:\n"
        "/rules - Activity and reward rules\n"
        "/points - Your current-cycle points\n"
        "/invite - Your personal invite link\n"
        "/rank - Weekly Top 20 and your rank"
    )


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    form_line = ""
    if setting("google_form_url"):
        form_line = f"\nReward form: {setting('google_form_url')}"
    await update.message.reply_text(
        "PerpVia Points Rules\n\n"
        f"Cycle: {setting_int('cycle_length_days')} days, UTC+8. Default week is Monday 00:00 "
        "through Sunday 23:59.\n"
        f"Chat: valid messages with at least {setting_int('valid_message_min_chars')} characters "
        f"earn {setting_int('chat_points_per_message')} Points each, capped at "
        f"{setting_int('daily_chat_points_cap')} Points per user per day.\n"
        f"Invites: an invitee must join through your personal link and send one valid message "
        f"within {setting_int('invite_valid_days')} days. Each valid invite earns "
        f"{setting_int('invite_points')} Points, capped at "
        f"{setting_int('daily_invite_points_cap')} invite Points per day.\n"
        "Invalid content includes pure emojis, pure links, forwarded content, repeated spam, "
        "ads, scams, sensitive words, and meaningless character stacking.\n\n"
        f"Rewards: users with at least {setting_int('weekly_reward_threshold')} weekly Points, "
        "active group membership, and no block/review flag qualify. Rewards are calculated as:\n"
        "user weekly points / qualified total points * weekly pool.\n"
        f"Weekly pool: {setting_decimal('weekly_reward_pool')}U. Rewards are generated as "
        "pending review and become official only after admin publication."
        f"{form_line}"
    )


async def cmd_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    cycle = current_cycle()
    cycle_id = cycle["cycle_id"]
    today = local_day()
    chat = points_sum(user.id, cycle_id, "chat")
    invite = points_sum(user.id, cycle_id, "invite")
    adjust = points_sum(user.id, cycle_id, "adjust")
    total = chat + invite + adjust
    threshold = setting_int("weekly_reward_threshold")
    need = max(0, threshold - total)
    chat_today = points_sum(user.id, cycle_id, "chat", today)
    invite_today = points_sum(user.id, cycle_id, "invite", today)
    chat_cap = setting_int("daily_chat_points_cap")
    invite_cap = setting_int("daily_invite_points_cap")

    if need:
        threshold_line = f"You need {need} more Points to qualify this week."
    else:
        threshold_line = "You have reached the weekly reward threshold."

    await update.message.reply_text(
        "Your PerpVia Points this week\n"
        f"Cycle: {format_cycle(cycle['start_at'], cycle['end_at'])}\n"
        f"Total: {total} Points\n"
        f"Chat: {chat} / Invite: {invite} / Raid-Adjust: {adjust}\n"
        f"Reward threshold: {threshold} Points\n"
        f"{threshold_line}\n\n"
        f"Today chat cap: {chat_today}/{chat_cap} Points "
        f"({'reached' if chat_today >= chat_cap else 'open'})\n"
        f"Today invite cap: {invite_today}/{invite_cap} Points "
        f"({'reached' if invite_today >= invite_cap else 'open'})"
    )


async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    cycle = current_cycle()
    threshold = setting_int("weekly_reward_threshold")
    rows = db_read(
        "SELECT u.user_id,u.username,u.first_name,u.last_name,u.is_blocked,"
        "COALESCE(SUM(p.points),0) AS pts "
        "FROM users u LEFT JOIN point_events p ON u.user_id=p.user_id AND p.cycle_id=? "
        "GROUP BY u.user_id ORDER BY pts DESC, u.user_id ASC",
        (cycle["cycle_id"],),
    )
    ranked = [r for r in rows if int(r["pts"]) > 0]
    lines = [
        f"PerpVia Weekly Rank - {format_cycle(cycle['start_at'], cycle['end_at'])}",
        "",
    ]
    if not ranked:
        lines.append("No points yet this cycle.")
    for idx, row in enumerate(ranked[:20], start=1):
        pts = int(row["pts"])
        if row["is_blocked"]:
            tag = " pending review"
        elif pts >= threshold:
            tag = " qualified"
        else:
            tag = ""
        lines.append(f"{idx}. {display_name(row)} - {pts} Points{tag}")

    my_rank = next((idx + 1 for idx, row in enumerate(ranked) if row["user_id"] == user.id), None)
    my_points = cycle_total_points(cycle["cycle_id"], user.id)
    lines.append("")
    if my_rank:
        lines.append(f"Your rank: #{my_rank} - {my_points} Points")
    else:
        lines.append(f"Your rank: not ranked yet - {my_points} Points")
    await update.message.reply_text("\n".join(lines))


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def cmd_admin_adjust(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=3)
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        upsert_user(target_user)
        if len(parts) < 2:
            await update.message.reply_text("Usage: reply to user + /admin_adjust <points> [reason]")
            return
        target = db_read_one("SELECT * FROM users WHERE user_id=?", (target_user.id,))
        points_token = parts[1]
        reason = parts[2] if len(parts) > 2 else "admin adjustment"
    else:
        if len(parts) < 3:
            await update.message.reply_text("Usage: /admin_adjust @username <points> [reason]")
            return
        target = resolve_user(parts[1])
        points_token = parts[2]
        reason = parts[3] if len(parts) > 3 else "admin adjustment"

    if not target:
        await update.message.reply_text("User not found. The user must have joined or used the bot first.")
        return
    try:
        points = int(points_token)
    except Exception:
        await update.message.reply_text("points must be a number, e.g. /admin_adjust @xxx -10")
        return

    event_id = record_point_event(
        target["user_id"],
        "adjust",
        points,
        f"admin:{update.effective_user.id}",
        reason,
    )
    await update.message.reply_text(
        f"Adjusted {display_name(target)} by {points} Points.\nEvent: {event_id}"
    )


async def cmd_admin_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_block @username")
        return
    target = resolve_user(ctx.args[0])
    if not target:
        await update.message.reply_text("User not found.")
        return
    db_write("UPDATE users SET is_blocked=1, updated_at=? WHERE user_id=?", (now_local().isoformat(), target["user_id"]))
    await update.message.reply_text(f"{display_name(target)} is now pending review / blocked from rewards.")


async def cmd_admin_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_unblock @username")
        return
    target = resolve_user(ctx.args[0])
    if not target:
        await update.message.reply_text("User not found.")
        return
    db_write("UPDATE users SET is_blocked=0, updated_at=? WHERE user_id=?", (now_local().isoformat(), target["user_id"]))
    await update.message.reply_text(f"{display_name(target)} is unblocked for reward calculations.")


async def cmd_admin_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        rows = db_read("SELECT key,value FROM settings ORDER BY key ASC")
        lines = ["Config values"]
        lines.extend(f"{r['key']} = {r['value']}" for r in rows)
        lines.append("\nSet with: /admin_config set <key> <value>")
        await update.message.reply_text("\n".join(lines[:80]))
        return
    if len(ctx.args) >= 3 and ctx.args[0].lower() == "set":
        key = ctx.args[1]
        value = " ".join(ctx.args[2:])
        if key not in DEFAULT_SETTINGS:
            await update.message.reply_text("Unknown config key.")
            return
        db_write(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_local().isoformat()),
        )
        await update.message.reply_text(f"Updated {key} = {value}")
        return
    await update.message.reply_text("Usage: /admin_config OR /admin_config set <key> <value>")


async def send_csv(update, filename, headers, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = filename
    await update.message.reply_document(document=data)


async def cmd_admin_export_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    cycle_filter = ctx.args[0] if ctx.args else None

    summary_sql = (
        "SELECT p.cycle_id,p.user_id,u.username,u.first_name,u.last_name,"
        "SUM(CASE WHEN p.event_type='chat' THEN p.points ELSE 0 END) AS chat_points,"
        "SUM(CASE WHEN p.event_type='invite' THEN p.points ELSE 0 END) AS invite_points,"
        "SUM(CASE WHEN p.event_type='adjust' THEN p.points ELSE 0 END) AS adjust_points,"
        "SUM(p.points) AS total_points "
        "FROM point_events p LEFT JOIN users u ON p.user_id=u.user_id"
    )
    summary_params = []
    if cycle_filter:
        summary_sql += " WHERE p.cycle_id=?"
        summary_params.append(cycle_filter)
    summary_sql += " GROUP BY p.cycle_id,p.user_id ORDER BY p.cycle_id ASC,total_points DESC"
    summary_rows = db_read(summary_sql, tuple(summary_params))
    await send_csv(
        update,
        f"perpvia_point_summary_{cycle_filter or 'all'}_{local_day()}.csv",
        [
            "cycle_id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "chat_points",
            "invite_points",
            "adjust_points",
            "total_points",
        ],
        [
            [
                r["cycle_id"],
                r["user_id"],
                r["username"],
                r["first_name"],
                r["last_name"],
                r["chat_points"],
                r["invite_points"],
                r["adjust_points"],
                r["total_points"],
            ]
            for r in summary_rows
        ],
    )

    sql = (
        "SELECT p.event_id,p.cycle_id,p.user_id,u.username,u.first_name,u.last_name,"
        "p.event_type,p.points,p.source_id,p.reason,p.created_at "
        "FROM point_events p LEFT JOIN users u ON p.user_id=u.user_id"
    )
    params = []
    if cycle_filter:
        sql += " WHERE p.cycle_id=?"
        params.append(cycle_filter)
    sql += " ORDER BY p.created_at ASC"
    rows = db_read(sql, tuple(params))
    await send_csv(
        update,
        f"perpvia_point_events_{cycle_filter or 'all'}_{local_day()}.csv",
        [
            "event_id",
            "cycle_id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "event_type",
            "points",
            "source_id",
            "reason",
            "created_at",
        ],
        [
            [
                r["event_id"],
                r["cycle_id"],
                r["user_id"],
                r["username"],
                r["first_name"],
                r["last_name"],
                r["event_type"],
                r["points"],
                r["source_id"],
                r["reason"],
                r["created_at"],
            ]
            for r in rows
        ],
    )


async def cmd_admin_export_invites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db_read(
        "SELECT i.invite_id,i.inviter_id,inviter.username AS inviter_username,"
        "i.invitee_id,invitee.username AS invitee_username,i.invite_link,i.joined_at,"
        "i.first_valid_message_at,i.status,i.points_awarded,i.awarded_points,i.note "
        "FROM invites i "
        "LEFT JOIN users inviter ON i.inviter_id=inviter.user_id "
        "LEFT JOIN users invitee ON i.invitee_id=invitee.user_id "
        "ORDER BY i.joined_at ASC"
    )
    await send_csv(
        update,
        f"perpvia_invites_{local_day()}.csv",
        [
            "invite_id",
            "inviter_id",
            "inviter_username",
            "invitee_id",
            "invitee_username",
            "invite_link",
            "joined_at",
            "first_valid_message_at",
            "status",
            "points_awarded",
            "awarded_points",
            "note",
        ],
        [
            [
                r["invite_id"],
                r["inviter_id"],
                r["inviter_username"],
                r["invitee_id"],
                r["invitee_username"],
                r["invite_link"],
                r["joined_at"],
                r["first_valid_message_at"],
                r["status"],
                r["points_awarded"],
                r["awarded_points"],
                r["note"],
            ]
            for r in rows
        ],
    )


async def cmd_admin_export_rewards(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await settle_due_cycles(ctx)
    cycle_filter = ctx.args[0] if ctx.args else None
    if not cycle_filter:
        latest = db_read_one("SELECT cycle_id FROM rewards ORDER BY created_at DESC LIMIT 1")
        cycle_filter = latest["cycle_id"] if latest else current_cycle()["cycle_id"]
    rows = db_read(
        "SELECT r.reward_id,r.cycle_id,r.user_id,u.username,u.first_name,u.last_name,"
        "r.weekly_points,r.reward_amount,r.status,r.published_at,r.created_at "
        "FROM rewards r LEFT JOIN users u ON r.user_id=u.user_id "
        "WHERE r.cycle_id=? ORDER BY CAST(r.reward_amount AS REAL) DESC",
        (cycle_filter,),
    )
    if not rows:
        await update.message.reply_text("No reward records found for that cycle yet.")
        return
    await send_csv(
        update,
        f"perpvia_rewards_{cycle_filter}_{local_day()}.csv",
        [
            "reward_id",
            "cycle_id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "weekly_points",
            "reward_amount_u",
            "status",
            "published_at",
            "created_at",
        ],
        [
            [
                r["reward_id"],
                r["cycle_id"],
                r["user_id"],
                r["username"],
                r["first_name"],
                r["last_name"],
                r["weekly_points"],
                r["reward_amount"],
                r["status"],
                r["published_at"],
                r["created_at"],
            ]
            for r in rows
        ],
    )


async def cmd_admin_publish_rewards(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await settle_due_cycles(ctx)
    cycle_id = ctx.args[0] if ctx.args else None
    if not cycle_id:
        row = db_read_one(
            "SELECT cycle_id FROM rewards WHERE status='pending_review' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        cycle_id = row["cycle_id"] if row else None
    if not cycle_id:
        await update.message.reply_text("No pending reward cycle found.")
        return

    rows = db_read(
        "SELECT r.*,u.username,u.first_name,u.last_name FROM rewards r "
        "LEFT JOIN users u ON r.user_id=u.user_id "
        "WHERE r.cycle_id=? ORDER BY r.weekly_points DESC",
        (cycle_id,),
    )
    if not rows:
        await update.message.reply_text("No rewards found for that cycle.")
        return

    published_at = now_local().isoformat()
    db_write(
        "UPDATE rewards SET status='published', published_at=? WHERE cycle_id=?",
        (published_at, cycle_id),
    )
    cycle = db_read_one("SELECT start_at,end_at FROM cycles WHERE cycle_id=?", (cycle_id,))
    title = f"PerpVia Weekly Rewards - {cycle_id}"
    if cycle:
        title = f"PerpVia Weekly Rewards ({format_cycle(parse_local(cycle['start_at']), parse_local(cycle['end_at']))})"
    lines = [title, ""]
    for idx, row in enumerate(rows[:50], start=1):
        lines.append(
            f"{idx}. {display_name(row)} - {row['weekly_points']} Points - {row['reward_amount']}U"
        )
    if len(rows) > 50:
        lines.append(f"...and {len(rows) - 50} more")
    if setting("google_form_url"):
        lines.append("")
        lines.append(f"Qualified users: submit reward info here: {setting('google_form_url')}")

    text = "\n".join(lines)
    if GROUP_CHAT_ID:
        await ctx.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        await update.message.reply_text(f"Published rewards for {cycle_id}.")
    else:
        await update.message.reply_text(text)


# ----------------------------------------------------------------------------
# Settlement
# ----------------------------------------------------------------------------
async def settle_due_cycles(ctx: ContextTypes.DEFAULT_TYPE):
    now = now_local()
    current = current_cycle()
    rows = db_read(
        "SELECT cycle_id,start_at,end_at FROM cycles WHERE status='open' AND end_at<?",
        (now.isoformat(),),
    )
    for row in rows:
        if row["cycle_id"] == current["cycle_id"]:
            continue
        await settle_cycle(ctx, row["cycle_id"])


async def settle_cycle(ctx: ContextTypes.DEFAULT_TYPE, cycle_id):
    cycle = db_read_one("SELECT * FROM cycles WHERE cycle_id=?", (cycle_id,))
    if not cycle or cycle["status"] != "open":
        return
    threshold = setting_int("weekly_reward_threshold")
    pool = setting_decimal("weekly_reward_pool")
    rows = db_read(
        "SELECT u.user_id,u.is_blocked,COALESCE(SUM(p.points),0) AS pts "
        "FROM users u JOIN point_events p ON u.user_id=p.user_id "
        "WHERE p.cycle_id=? GROUP BY u.user_id HAVING pts>=?",
        (cycle_id, threshold),
    )

    eligible = []
    for row in rows:
        if row["is_blocked"]:
            continue
        if await is_group_member(ctx.bot, row["user_id"]):
            eligible.append((row["user_id"], int(row["pts"])))

    total_points = sum(points for _, points in eligible)
    db_write("DELETE FROM rewards WHERE cycle_id=? AND status='pending_review'", (cycle_id,))
    if total_points > 0:
        reward_rows = []
        for user_id, weekly_points in eligible:
            amount = (
                Decimal(weekly_points) / Decimal(total_points) * pool
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            reward_rows.append(
                (
                    uuid.uuid4().hex,
                    cycle_id,
                    user_id,
                    weekly_points,
                    str(amount),
                    "pending_review",
                    now_local().isoformat(),
                )
            )
        db_many(
            "INSERT OR REPLACE INTO rewards "
            "(reward_id,cycle_id,user_id,weekly_points,reward_amount,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            reward_rows,
        )
    db_write(
        "UPDATE cycles SET status='settled', settled_at=? WHERE cycle_id=?",
        (now_local().isoformat(), cycle_id),
    )
    log.info("Settled cycle %s with %s eligible reward(s)", cycle_id, len(eligible))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("points", cmd_points))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("rank", cmd_rank))

    app.add_handler(CommandHandler("admin_adjust", cmd_admin_adjust))
    app.add_handler(CommandHandler("admin_export_points", cmd_admin_export_points))
    app.add_handler(CommandHandler("admin_export_invites", cmd_admin_export_invites))
    app.add_handler(CommandHandler("admin_export_rewards", cmd_admin_export_rewards))
    app.add_handler(CommandHandler("admin_publish_rewards", cmd_admin_publish_rewards))
    app.add_handler(CommandHandler("admin_config", cmd_admin_config))
    app.add_handler(CommandHandler("admin_block", cmd_admin_block))
    app.add_handler(CommandHandler("admin_unblock", cmd_admin_unblock))

    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_repeating(settle_due_cycles, interval=3600, first=60)
    app.job_queue.run_repeating(expire_pending_invites, interval=3600, first=120)

    log.info("PerpVia PRD V1.0 bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
