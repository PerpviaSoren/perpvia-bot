# -*- coding: utf-8 -*-
"""
PerpVia Community Points Telegram Bot
PRD V1.0 implementation

Core rules:
- Weekly UTC cycles, default Monday 00:00 through Sunday 23:59.
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

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    Update,
)
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


def parse_admin_ids(raw_value):
    tokens = re.findall(r"\d+", raw_value or "")
    return {int(token) for token in tokens}


ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
GROUP_USERNAME = os.environ.get("GROUP_USERNAME", "PerpViaPioneerHub").lstrip("@")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0") or 0)
DB_PATH = os.environ.get("DB_PATH", "perpvia.db")

UTC = dt.timezone.utc

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
    "perpvia_reply_text": os.environ.get("PERPVIA_REPLY_TEXT", ""),
    "rules_reply_text": os.environ.get("RULES_REPLY_TEXT", ""),
}

INTEGER_SETTING_RANGES = {
    "cycle_length_days": (1, 365),
    "cycle_start_hour": (0, 23),
    "cycle_start_minute": (0, 59),
    "valid_message_min_chars": (1, 1000),
    "chat_points_per_message": (1, 10000),
    "daily_chat_points_cap": (1, 100000),
    "invite_points": (1, 10000),
    "daily_invite_points_cap": (1, 100000),
    "invite_valid_days": (1, 365),
    "weekly_reward_threshold": (1, 10000000),
    "min_seconds_between_valid_messages": (0, 86400),
    "duplicate_message_window_minutes": (0, 525600),
}
CYCLE_SETTING_KEYS = {
    "cycle_length_days",
    "cycle_anchor_date",
    "cycle_start_hour",
    "cycle_start_minute",
}
SCORING_SETTING_KEYS = {
    "valid_message_min_chars",
    "chat_points_per_message",
    "daily_chat_points_cap",
    "invite_points",
    "daily_invite_points_cap",
    "invite_valid_days",
    "weekly_reward_threshold",
    "weekly_reward_pool",
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
            blocked_reason TEXT,
            blocked_at TEXT,
            blocked_by INTEGER,
            personal_invite_link TEXT,
            created_at TEXT,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS cycles (
            cycle_id TEXT PRIMARY KEY,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            reward_threshold INTEGER,
            reward_pool TEXT,
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
        """CREATE TABLE IF NOT EXISTS admin_actions (
            action_id TEXT PRIMARY KEY,
            admin_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            target_user_id INTEGER,
            cycle_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS risk_flags (
            flag_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            flag_type TEXT NOT NULL,
            source_id TEXT,
            details TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER
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
        ("blocked_reason", "TEXT"),
        ("blocked_at", "TEXT"),
        ("blocked_by", "INTEGER"),
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
    try_add_column("cycles", "reward_threshold", "INTEGER")
    try_add_column("cycles", "reward_pool", "TEXT")

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_point_events_cycle_user ON point_events(cycle_id,user_id)",
        "CREATE INDEX IF NOT EXISTS idx_point_events_user_type_created ON point_events(user_id,event_type,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_invites_invitee_status ON invites(invitee_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_invites_inviter_joined ON invites(inviter_id,joined_at)",
        "CREATE INDEX IF NOT EXISTS idx_invites_status_joined ON invites(status,joined_at)",
        "CREATE INDEX IF NOT EXISTS idx_message_audit_user_hash_created ON message_audit(user_id,text_hash,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_message_audit_cycle_valid ON message_audit(cycle_id,valid)",
        "CREATE INDEX IF NOT EXISTS idx_rewards_cycle_status ON rewards(cycle_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_risk_flags_user_status ON risk_flags(user_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_admin_actions_created ON admin_actions(created_at)",
    ]
    for statement in indexes:
        db_write(statement)

    now = now_local().isoformat()
    for key, value in DEFAULT_SETTINGS.items():
        db_write(
            "INSERT OR IGNORE INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (key, str(value), now),
        )
    db_write(
        "UPDATE cycles SET reward_threshold=COALESCE(reward_threshold,?), "
        "reward_pool=COALESCE(reward_pool,?)",
        (setting_int("weekly_reward_threshold"), str(setting_decimal("weekly_reward_pool"))),
    )
    ensure_cycle(*current_cycle_bounds())
    log.info("DB initialised")


# ----------------------------------------------------------------------------
# Time, settings, and cycles
# ----------------------------------------------------------------------------
def now_local():
    return dt.datetime.now(UTC).replace(microsecond=0)


def parse_local(iso_value):
    if not iso_value:
        return None
    value = dt.datetime.fromisoformat(iso_value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def setting(key):
    row = db_read_one("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        return row["value"]
    return DEFAULT_SETTINGS[key]


def setting_int(key):
    return int(setting(key))


def setting_decimal(key):
    return Decimal(str(setting(key)))


def validate_setting_value(key, value):
    if key not in DEFAULT_SETTINGS:
        return False, "Unknown config key.", None
    value = str(value).strip()
    if key in INTEGER_SETTING_RANGES:
        try:
            parsed = int(value)
        except ValueError:
            return False, f"{key} must be an integer.", None
        minimum, maximum = INTEGER_SETTING_RANGES[key]
        if not minimum <= parsed <= maximum:
            return False, f"{key} must be between {minimum} and {maximum}.", None
        return True, "", str(parsed)
    if key == "cycle_anchor_date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return False, "cycle_anchor_date must use YYYY-MM-DD.", None
        return True, "", value
    if key == "weekly_reward_pool":
        try:
            parsed = Decimal(value)
        except Exception:
            return False, "weekly_reward_pool must be a number.", None
        if parsed <= 0 or parsed > Decimal("100000000"):
            return False, "weekly_reward_pool must be greater than 0 and at most 100000000.", None
        return True, "", format(parsed.normalize(), "f")
    if key == "google_form_url":
        if value and not re.match(r"^https://", value, re.IGNORECASE):
            return False, "google_form_url must be empty or start with https://.", None
        return True, "", value
    if key == "sensitive_words":
        if len(value) > 10000:
            return False, "sensitive_words is too long.", None
        return True, "", value
    if key in {"perpvia_reply_text", "rules_reply_text"}:
        if len(value) > 4000:
            return False, f"{key} must be 4000 characters or fewer.", None
        return True, "", value
    return True, "", value


def validate_all_settings():
    errors = []
    for key in DEFAULT_SETTINGS:
        valid, message, _ = validate_setting_value(key, setting(key))
        if not valid:
            errors.append(message)
    if not errors:
        if setting_int("chat_points_per_message") > setting_int("daily_chat_points_cap"):
            errors.append("chat_points_per_message cannot exceed daily_chat_points_cap.")
        if setting_int("invite_points") > setting_int("daily_invite_points_cap"):
            errors.append("invite_points cannot exceed daily_invite_points_cap.")
    return errors


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
            tzinfo=UTC,
        ),
    )
    seconds_per_cycle = length_days * 86400
    index = math.floor((at - anchor).total_seconds() / seconds_per_cycle)
    start_at = anchor + dt.timedelta(days=index * length_days)
    end_at = start_at + dt.timedelta(days=length_days) - dt.timedelta(seconds=1)
    return cycle_id_from_start(start_at), start_at, end_at


def ensure_cycle(cycle_id, start_at, end_at):
    db_write(
        "INSERT OR IGNORE INTO cycles "
        "(cycle_id,start_at,end_at,status,reward_threshold,reward_pool,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            cycle_id,
            start_at.isoformat(),
            end_at.isoformat(),
            "open",
            setting_int("weekly_reward_threshold"),
            str(setting_decimal("weekly_reward_pool")),
            now_local().isoformat(),
        ),
    )
    db_write(
        "UPDATE cycles SET start_at=?,end_at=? WHERE cycle_id=? AND status='open'",
        (start_at.isoformat(), end_at.isoformat(), cycle_id),
    )


def current_cycle():
    cycle_id, start_at, end_at = current_cycle_bounds()
    ensure_cycle(cycle_id, start_at, end_at)
    return {"cycle_id": cycle_id, "start_at": start_at, "end_at": end_at}


def cycle_reward_rules(cycle_id):
    row = db_read_one(
        "SELECT reward_threshold,reward_pool FROM cycles WHERE cycle_id=?", (cycle_id,)
    )
    threshold = (
        int(row["reward_threshold"])
        if row and row["reward_threshold"] is not None
        else setting_int("weekly_reward_threshold")
    )
    pool = (
        Decimal(str(row["reward_pool"]))
        if row and row["reward_pool"] is not None
        else setting_decimal("weekly_reward_pool")
    )
    return threshold, pool


def format_day(value):
    return value.strftime("%b ") + str(value.day)


def format_cycle(start_at, end_at):
    return f"{format_day(start_at)} - {format_day(end_at)}"


def local_day(at=None):
    return (at or now_local()).date().isoformat()


# ----------------------------------------------------------------------------
# User and scoring helpers
# ----------------------------------------------------------------------------
def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_allowed_chat(update):
    chat = update.effective_chat
    if not chat:
        return False
    if chat.type == "private":
        return True
    return bool(GROUP_CHAT_ID and chat.id == GROUP_CHAT_ID)


async def require_allowed_chat(update):
    if is_allowed_chat(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "This bot is not available in this group."
        )
    return False


async def require_admin(update):
    if not await require_allowed_chat(update):
        return False
    user = update.effective_user
    if user and is_admin(user.id):
        return True
    if update.effective_message and user:
        await update.effective_message.reply_text(
            "Admin access denied.\n"
            f"Your Telegram user ID: {user.id}\n"
            "Add this numeric ID to the ADMIN_IDS environment variable and restart the bot."
        )
    return False


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
    cycle = db_read_one("SELECT status FROM cycles WHERE cycle_id=?", (cycle_id,))
    if not cycle or cycle["status"] != "open":
        log.warning(
            "Rejected point event for frozen cycle cycle=%s user=%s type=%s",
            cycle_id,
            user_id,
            event_type,
        )
        return None
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


def record_admin_action(admin_id, action_type, target_user_id=None, cycle_id=None, details=""):
    action_id = uuid.uuid4().hex
    db_write(
        "INSERT INTO admin_actions "
        "(action_id,admin_id,action_type,target_user_id,cycle_id,details,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            action_id,
            admin_id,
            action_type,
            target_user_id,
            cycle_id,
            details,
            now_local().isoformat(),
        ),
    )
    return action_id


def create_risk_flag(user_id, flag_type, source_id=None, details=""):
    if source_id:
        existing = db_read_one(
            "SELECT flag_id FROM risk_flags WHERE user_id=? AND flag_type=? "
            "AND source_id=? AND status='open'",
            (user_id, flag_type, source_id),
        )
        if existing:
            return existing["flag_id"]
    flag_id = uuid.uuid4().hex
    db_write(
        "INSERT INTO risk_flags "
        "(flag_id,user_id,flag_type,source_id,details,status,created_at) "
        "VALUES (?,?,?,?,?,'open',?)",
        (flag_id, user_id, flag_type, source_id, details, now_local().isoformat()),
    )
    return flag_id


def remaining_time_text(end_at):
    remaining = max(dt.timedelta(0), end_at - now_local())
    total_seconds = int(remaining.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours = remainder // 3600
    if days:
        return f"{days}d {hours}h"
    minutes = (remainder % 3600) // 60
    return f"{hours}h {minutes}m"


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


def low_quality_text(text):
    compact = re.sub(r"\s+", "", text)
    if len(URL_RE.findall(text)) >= 3:
        return "excessive_links"
    if compact:
        meaningful = sum(ch.isalnum() for ch in compact)
        if len(compact) >= 20 and meaningful / len(compact) < 0.3:
            return "symbol_stacking"
    words = re.findall(r"[\w']+", text.lower(), flags=re.UNICODE)
    if len(words) >= 8 and len(set(words)) <= 2:
        return "repeated_words"
    return None


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
    quality_issue = low_quality_text(stripped)
    if quality_issue:
        return False, quality_issue, text_hash(stripped)

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
    if not is_allowed_chat(update) or update.effective_chat.type == "private":
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
        requested_award = min(per_message, cap - earned_today)
        event_id = record_point_event(
            user.id,
            "chat",
            requested_award,
            f"{update.effective_chat.id}:{update.message.message_id}",
            "valid chat message",
        )
        if event_id:
            award = requested_award
            score_reason = "valid_chat_scored"
        else:
            score_reason = "cycle_frozen"

    log_message(update, cycle["cycle_id"], day, h, True, award, score_reason)
    await maybe_validate_invite(ctx, user.id)


# ----------------------------------------------------------------------------
# Invite tracking and scoring
# ----------------------------------------------------------------------------
async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
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
        create_risk_flag(
            user.id,
            "self_invite_attempt",
            invite_link,
            "User attempted to join through their own invite link.",
        )
        inviter_id = None

    upsert_user(user, joined_at=joined_at, invited_by=inviter_id)
    if not inviter_id:
        return
    if user.is_bot:
        invite_id = uuid.uuid4().hex
        db_write(
            "INSERT INTO invites "
            "(invite_id,inviter_id,invitee_id,invite_link,joined_at,status,note,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'rejected',?,?,?)",
            (
                invite_id,
                inviter_id,
                user.id,
                invite_link,
                joined_at.isoformat(),
                "Bot accounts are not valid invitees.",
                now_local().isoformat(),
                now_local().isoformat(),
            ),
        )
        create_risk_flag(
            inviter_id,
            "bot_invitee",
            invite_id,
            f"Invitee {user.id} is a bot account.",
        )
        return

    previous = db_read_one(
        "SELECT invite_id,status FROM invites WHERE invitee_id=? ORDER BY created_at ASC LIMIT 1",
        (user.id,),
    )
    if previous:
        return


    inviter = db_read_one("SELECT is_blocked FROM users WHERE user_id=?", (inviter_id,))
    if inviter and inviter["is_blocked"]:
        db_write(
            "INSERT INTO invites "
            "(invite_id,inviter_id,invitee_id,invite_link,joined_at,status,note,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'rejected',?,?,?)",
            (
                uuid.uuid4().hex,
                inviter_id,
                user.id,
                invite_link,
                joined_at.isoformat(),
                "Inviter is blocked or pending review.",
                now_local().isoformat(),
                now_local().isoformat(),
            ),
        )
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

    burst_cutoff = joined_at - dt.timedelta(minutes=10)
    burst = db_read_one(
        "SELECT COUNT(*) AS n FROM invites WHERE inviter_id=? AND joined_at>=?",
        (inviter_id, burst_cutoff.isoformat()),
    )
    if burst and int(burst["n"]) >= 5:
        create_risk_flag(
            inviter_id,
            "invite_burst",
            f"{inviter_id}:{joined_at.strftime('%Y%m%d%H%M')}",
            f"{burst['n']} invite joins were recorded within 10 minutes.",
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


    inviter = db_read_one(
        "SELECT is_blocked FROM users WHERE user_id=?", (invite["inviter_id"],)
    )
    if inviter and inviter["is_blocked"]:
        db_write(
            "UPDATE invites SET status='rejected', first_valid_message_at=?, note=?, "
            "updated_at=? WHERE invite_id=?",
            (
                now.isoformat(),
                "Inviter is blocked or pending review.",
                now.isoformat(),
                invite["invite_id"],
            ),
        )
        return

    cycle = current_cycle()
    earned_today = points_sum(invite["inviter_id"], cycle["cycle_id"], "invite", local_day(now))
    invite_points = setting_int("invite_points")
    daily_cap = setting_int("daily_invite_points_cap")
    award = invite_points if earned_today + invite_points <= daily_cap else 0
    invite_note = "Valid invite, daily invite cap reached."

    if award:
        event_id = record_point_event(
            invite["inviter_id"],
            "invite",
            award,
            invite["invite_id"],
            f"valid invite: {invitee_id}",
            now,
        )
        if not event_id:
            award = 0
            invite_note = "Valid invite, but the scoring cycle is frozen."
        else:
            invite_note = "Valid invite scored."
    db_write(
        "UPDATE invites SET status='valid', first_valid_message_at=?, points_awarded=?, "
        "awarded_points=?, note=?, updated_at=? WHERE invite_id=?",
        (
            now.isoformat(),
            int(bool(award)),
            award,
            invite_note,
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
def default_perpvia_reply():
    return (
        "Welcome to PerpVia Community Points.\n\n"
        "Earn weekly points through valid community chat, valid invites, and occasional "
        "admin-scored raid tasks. Weekly qualified users share the 300U contract-trial "
        "reward pool proportionally.\n\n"
        "Commands:\n"
        "/points - View your weekly progress\n"
        "/rank - View the weekly leaderboard\n"
        "/invite - Get your personal invite link\n"
        "/rules - Read the activity and reward rules"
    )


def default_rules_reply():
    form_line = ""
    if setting("google_form_url"):
        form_line = f"\nReward form: {setting('google_form_url')}"
    return (
        "PerpVia Points Rules\n\n"
        f"Cycle\n{setting_int('cycle_length_days')} days, normally Monday 00:00 to "
        "Sunday 23:59.\n\n"
        f"Chat points\nA valid message of at least {setting_int('valid_message_min_chars')} characters "
        f"earns {setting_int('chat_points_per_message')} Points. Daily limit: "
        f"{setting_int('daily_chat_points_cap')} Points.\n\n"
        f"Invite points\nAn invitee must join through your personal link and post one valid "
        f"message within {setting_int('invite_valid_days')} days. Each valid invite earns "
        f"{setting_int('invite_points')} Points. Daily limit: "
        f"{setting_int('daily_invite_points_cap')} Points.\n\n"
        "Content review\nPure emojis, pure links, forwards, repeated spam, ads, scams, sensitive "
        "content, and meaningless text do not earn Points.\n\n"
        f"Weekly rewards\nReach {setting_int('weekly_reward_threshold')} Points and remain an active "
        f"group member to qualify for a share of the {setting_decimal('weekly_reward_pool')}U pool. "
        "Rewards are proportional to qualified users' Points and become official only after "
        "admin review and publication."
        f"{form_line}"
    )


async def cmd_perpvia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    upsert_user(update.effective_user)
    await update.message.reply_text(setting("perpvia_reply_text") or default_perpvia_reply())


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    await update.message.reply_text(setting("rules_reply_text") or default_rules_reply())


async def cmd_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    user = update.effective_user
    upsert_user(user)
    cycle = current_cycle()
    cycle_id = cycle["cycle_id"]
    today = local_day()
    chat = points_sum(user.id, cycle_id, "chat")
    invite = points_sum(user.id, cycle_id, "invite")
    adjust = points_sum(user.id, cycle_id, "adjust")
    total = chat + invite + adjust
    threshold, _ = cycle_reward_rules(cycle_id)
    need = max(0, threshold - total)
    chat_today = points_sum(user.id, cycle_id, "chat", today)
    invite_today = points_sum(user.id, cycle_id, "invite", today)
    chat_cap = setting_int("daily_chat_points_cap")
    invite_cap = setting_int("daily_invite_points_cap")
    invite_counts = db_read_one(
        "SELECT "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,"
        "SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) AS valid_count "
        "FROM invites WHERE inviter_id=?",
        (user.id,),
    )
    pending_invites = int(invite_counts["pending_count"] or 0)
    valid_invites = int(invite_counts["valid_count"] or 0)

    if need:
        threshold_line = f"You need {need} more Points to qualify this week."
    else:
        threshold_line = "You have reached the weekly reward threshold."

    await update.message.reply_text(
        "Your PerpVia Points\n"
        f"Cycle: {format_cycle(cycle['start_at'], cycle['end_at'])}\n"
        f"Time remaining: {remaining_time_text(cycle['end_at'])}\n\n"
        f"Total: {total} Points\n"
        f"Chat: {chat} | Invite: {invite} | Bonus/Adjust: {adjust}\n"
        f"Reward threshold: {threshold} Points\n"
        f"{threshold_line}\n\n"
        f"Today: chat {chat_today}/{chat_cap} | invites {invite_today}/{invite_cap}\n"
        f"Invites: {valid_invites} valid | {pending_invites} pending"
    )


async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    user = update.effective_user
    upsert_user(user)
    cycle = current_cycle()
    threshold, _ = cycle_reward_rules(cycle["cycle_id"])
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
            tag = " [Pending review]"
        elif pts >= threshold:
            tag = " [Qualified]"
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
    if my_points < threshold:
        lines.append(f"To qualify: {threshold - my_points} more Points")
    else:
        lines.append("Threshold status: reached")
    await update.message.reply_text("\n".join(lines))


# ----------------------------------------------------------------------------
# Admin commands
# ----------------------------------------------------------------------------
async def cmd_admin_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(
        f"Your Telegram user ID: {user.id}\n"
        f"Admin access: {'enabled' if is_admin(user.id) else 'disabled'}\n"
        "Environment variable: ADMIN_IDS"
    )


async def cmd_admin_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    await update.message.reply_text(
        "PerpVia Admin Commands\n\n"
        "/admin_whoami - Check your admin access\n"
        "/admin_adjust @username <points> [reason] - Adjust Points\n"
        "/admin_set_perpvia <text> - Configure /perpvia reply\n"
        "/admin_set_rules <text> - Configure /rules reply\n"
        "/admin_export_points [cycle_id] - Export point reports\n"
        "/admin_export_invites - Export invite records\n"
        "/admin_export_rewards [cycle_id] - Export rewards\n"
        "/admin_stats [cycle_id] - View operations stats\n"
        "/admin_cycle - View cycle status\n"
        "/admin_user @username - Review a user\n"
        "/admin_invites [@username] - Review invites\n"
        "/admin_settle_preview [cycle_id] - Preview settlement\n"
        "/admin_publish_rewards [cycle_id] - Publish rewards\n"
        "/admin_block @username [reason] - Block rewards\n"
        "/admin_unblock @username [reason] - Restore eligibility\n"
        "/admin_risks [@username] - View risk flags\n"
        "/admin_config - View activity configuration"
    )


def configurable_reply_text(update, ctx):
    if ctx.args and len(ctx.args) == 1 and ctx.args[0].lower() == "reset":
        return "reset", ""
    replied = update.message.reply_to_message if update.message else None
    if replied and (replied.text or replied.caption):
        return "set", (replied.text or replied.caption).strip()
    text = update.message.text if update.message else ""
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return "set", parts[1].strip()
    return "missing", ""


async def set_configurable_reply(update, ctx, setting_key, public_command):
    if not await require_admin(update):
        return
    action, value = configurable_reply_text(update, ctx)
    admin_command = update.message.text.split(maxsplit=1)[0].split("@")[0]
    if action == "missing":
        await update.message.reply_text(
            f"Usage: {admin_command} <reply text>\n"
            f"You can also reply to a text message with {admin_command}.\n"
            f"Reset: {admin_command} reset"
        )
        return
    if action == "reset":
        value = ""
    valid, message, normalized = validate_setting_value(setting_key, value)
    if not valid:
        await update.message.reply_text(message)
        return
    db_write(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (setting_key, normalized, now_local().isoformat()),
    )
    action_id = record_admin_action(
        update.effective_user.id,
        "reset_reply_text" if action == "reset" else "update_reply_text",
        details=f"command={public_command}; setting={setting_key}",
    )
    status = "restored to its default reply" if action == "reset" else "updated"
    await update.message.reply_text(
        f"{public_command} reply has been {status}.\nAudit: {action_id}"
    )


async def cmd_admin_set_perpvia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_configurable_reply(update, ctx, "perpvia_reply_text", "/perpvia")


async def cmd_admin_set_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_configurable_reply(update, ctx, "rules_reply_text", "/rules")


async def cmd_admin_adjust(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
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
        reason = " ".join(parts[2:]) if len(parts) > 2 else "admin adjustment"
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
    if points == 0:
        await update.message.reply_text("Adjustment must be greater or less than zero.")
        return

    event_id = record_point_event(
        target["user_id"],
        "adjust",
        points,
        f"admin:{update.effective_user.id}",
        reason,
    )
    if not event_id:
        await update.message.reply_text("The current cycle is frozen. No adjustment was recorded.")
        return
    action_id = record_admin_action(
        update.effective_user.id,
        "adjust_points",
        target["user_id"],
        current_cycle()["cycle_id"],
        f"points={points}; event_id={event_id}; reason={reason}",
    )
    await update.message.reply_text(
        f"Adjusted {display_name(target)} by {points} Points.\n"
        f"Event: {event_id}\nAudit: {action_id}"
    )


async def cmd_admin_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_block @username [reason]")
        return
    target = resolve_user(ctx.args[0])
    if not target:
        await update.message.reply_text("User not found.")
        return
    reason = " ".join(ctx.args[1:]).strip() or "Manual admin review"
    now = now_local().isoformat()
    db_write(
        "UPDATE users SET is_blocked=1, blocked_reason=?, blocked_at=?, blocked_by=?, "
        "updated_at=? WHERE user_id=?",
        (reason, now, update.effective_user.id, now, target["user_id"]),
    )
    action_id = record_admin_action(
        update.effective_user.id,
        "block_user",
        target["user_id"],
        current_cycle()["cycle_id"],
        reason,
    )
    await update.message.reply_text(
        f"{display_name(target)} is pending review and excluded from rewards.\n"
        f"Reason: {reason}\nAudit: {action_id}"
    )


async def cmd_admin_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_unblock @username")
        return
    target = resolve_user(ctx.args[0])
    if not target:
        await update.message.reply_text("User not found.")
        return
    reason = " ".join(ctx.args[1:]).strip() or "Review completed"
    now = now_local().isoformat()
    db_write(
        "UPDATE users SET is_blocked=0, blocked_reason=NULL, blocked_at=NULL, "
        "blocked_by=NULL, updated_at=? WHERE user_id=?",
        (now, target["user_id"]),
    )
    action_id = record_admin_action(
        update.effective_user.id,
        "unblock_user",
        target["user_id"],
        current_cycle()["cycle_id"],
        reason,
    )
    await update.message.reply_text(
        f"{display_name(target)} is eligible for reward calculations again.\nAudit: {action_id}"
    )


async def cmd_admin_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not ctx.args:
        rows = db_read("SELECT key,value FROM settings ORDER BY key ASC")
        lines = ["Config values"]
        lines.extend(
            f"{r['key']} = "
            f"{'<custom text configured>' if r['key'] in {'perpvia_reply_text', 'rules_reply_text'} and r['value'] else r['value']}"
            for r in rows
        )
        lines.append("\nSet with: /admin_config set <key> <value>")
        await update.message.reply_text("\n".join(lines[:80]))
        return
    if len(ctx.args) >= 3 and ctx.args[0].lower() == "set":
        key = ctx.args[1]
        value = " ".join(ctx.args[2:])
        valid, message, normalized = validate_setting_value(key, value)
        if not valid:
            await update.message.reply_text(message)
            return
        if key in (CYCLE_SETTING_KEYS | SCORING_SETTING_KEYS) and normalized != setting(key):
            cycle = current_cycle()
            event_count = db_read_one(
                "SELECT COUNT(*) AS n FROM point_events WHERE cycle_id=?",
                (cycle["cycle_id"],),
            )
            if event_count and int(event_count["n"]) > 0:
                await update.message.reply_text(
                    "Cycle and scoring rules cannot be changed after the current cycle has point events."
                )
                return
        candidate_chat_points = (
            int(normalized) if key == "chat_points_per_message" else setting_int("chat_points_per_message")
        )
        candidate_chat_cap = (
            int(normalized) if key == "daily_chat_points_cap" else setting_int("daily_chat_points_cap")
        )
        candidate_invite_points = (
            int(normalized) if key == "invite_points" else setting_int("invite_points")
        )
        candidate_invite_cap = (
            int(normalized) if key == "daily_invite_points_cap" else setting_int("daily_invite_points_cap")
        )
        if candidate_chat_points > candidate_chat_cap:
            await update.message.reply_text(
                "chat_points_per_message cannot exceed daily_chat_points_cap."
            )
            return
        if candidate_invite_points > candidate_invite_cap:
            await update.message.reply_text(
                "invite_points cannot exceed daily_invite_points_cap."
            )
            return
        old_value = setting(key)
        db_write(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, normalized, now_local().isoformat()),
        )
        if key in {"weekly_reward_threshold", "weekly_reward_pool"}:
            cycle = current_cycle()
            column = "reward_threshold" if key == "weekly_reward_threshold" else "reward_pool"
            db_write(
                f"UPDATE cycles SET {column}=? WHERE cycle_id=? AND status='open'",
                (normalized, cycle["cycle_id"]),
            )
        action_id = record_admin_action(
            update.effective_user.id,
            "update_config",
            details=f"{key}: {old_value} -> {normalized}",
        )
        await update.message.reply_text(f"Updated {key} = {normalized}\nAudit: {action_id}")
        return
    await update.message.reply_text("Usage: /admin_config OR /admin_config set <key> <value>")


async def cmd_admin_cycle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    cycle = current_cycle()
    rows = db_read(
        "SELECT c.cycle_id,c.start_at,c.end_at,c.status,c.settled_at,"
        "COUNT(p.event_id) AS event_count,COALESCE(SUM(p.points),0) AS total_points "
        "FROM cycles c LEFT JOIN point_events p ON c.cycle_id=p.cycle_id "
        "GROUP BY c.cycle_id ORDER BY c.start_at DESC LIMIT 6"
    )
    lines = [
        "Cycle status",
        f"Current: {cycle['cycle_id']}",
        f"Window: {format_cycle(cycle['start_at'], cycle['end_at'])}",
        f"Time remaining: {remaining_time_text(cycle['end_at'])}",
        "",
        "Recent cycles:",
    ]
    for row in rows:
        lines.append(
            f"{row['cycle_id']} | {row['status']} | {row['event_count']} events | "
            f"{row['total_points']} Points"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    cycle = current_cycle()
    cycle_id = ctx.args[0] if ctx.args else cycle["cycle_id"]
    selected_cycle = db_read_one("SELECT * FROM cycles WHERE cycle_id=?", (cycle_id,))
    if not selected_cycle:
        await update.message.reply_text("Cycle not found.")
        return
    points = db_read_one(
        "SELECT COUNT(DISTINCT user_id) AS users,COALESCE(SUM(points),0) AS total,"
        "COALESCE(SUM(CASE WHEN event_type='chat' THEN points ELSE 0 END),0) AS chat,"
        "COALESCE(SUM(CASE WHEN event_type='invite' THEN points ELSE 0 END),0) AS invite,"
        "COALESCE(SUM(CASE WHEN event_type='adjust' THEN points ELSE 0 END),0) AS adjust "
        "FROM point_events WHERE cycle_id=?",
        (cycle_id,),
    )
    messages = db_read_one(
        "SELECT COUNT(*) AS total,COALESCE(SUM(valid),0) AS valid FROM message_audit "
        "WHERE cycle_id=?",
        (cycle_id,),
    )
    invite_stats = db_read_one(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
        "SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) AS valid,"
        "SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,"
        "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected "
        "FROM invites WHERE joined_at>=? AND joined_at<=?",
        (selected_cycle["start_at"], selected_cycle["end_at"]),
    )
    threshold_candidates = db_read_one(
        "SELECT COUNT(*) AS n FROM (SELECT user_id,SUM(points) AS pts FROM point_events "
        "WHERE cycle_id=? GROUP BY user_id HAVING pts>=?)",
        (cycle_id, cycle_reward_rules(cycle_id)[0]),
    )
    operational = db_read_one(
        "SELECT (SELECT COUNT(*) FROM users WHERE is_blocked=1) AS blocked,"
        "(SELECT COUNT(*) FROM risk_flags WHERE status='open') AS open_flags,"
        "(SELECT COUNT(*) FROM users) AS all_users"
    )
    valid_messages = int(messages["valid"] or 0)
    total_messages = int(messages["total"] or 0)
    lines = [
        f"PerpVia Operations - {cycle_id}",
        f"Status: {selected_cycle['status']}",
        "",
        f"Known users: {operational['all_users']}",
        f"Users with Points: {points['users']}",
        f"Points: {points['total']} total | {points['chat']} chat | "
        f"{points['invite']} invite | {points['adjust']} adjust",
        f"Messages: {valid_messages} valid | {total_messages - valid_messages} rejected",
        f"Invites: {invite_stats['valid'] or 0} valid | {invite_stats['pending'] or 0} pending | "
        f"{invite_stats['expired'] or 0} expired | {invite_stats['rejected'] or 0} rejected",
        f"Threshold candidates: {threshold_candidates['n']}",
        f"Review queue: {operational['blocked']} blocked | {operational['open_flags']} open flags",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_admin_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_user @username")
        return
    target = resolve_user(ctx.args[0])
    if not target:
        await update.message.reply_text("User not found.")
        return
    cycle = current_cycle()
    breakdown = db_read_one(
        "SELECT COALESCE(SUM(points),0) AS total,"
        "COALESCE(SUM(CASE WHEN event_type='chat' THEN points ELSE 0 END),0) AS chat,"
        "COALESCE(SUM(CASE WHEN event_type='invite' THEN points ELSE 0 END),0) AS invite,"
        "COALESCE(SUM(CASE WHEN event_type='adjust' THEN points ELSE 0 END),0) AS adjust "
        "FROM point_events WHERE cycle_id=? AND user_id=?",
        (cycle["cycle_id"], target["user_id"]),
    )
    invite_stats = db_read_one(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
        "SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) AS valid "
        "FROM invites WHERE inviter_id=?",
        (target["user_id"],),
    )
    audit = db_read_one(
        "SELECT COUNT(*) AS total,COALESCE(SUM(valid),0) AS valid FROM message_audit "
        "WHERE cycle_id=? AND user_id=?",
        (cycle["cycle_id"], target["user_id"]),
    )
    flags = db_read_one(
        "SELECT COUNT(*) AS n FROM risk_flags WHERE user_id=? AND status='open'",
        (target["user_id"],),
    )
    valid_messages = int(audit["valid"] or 0)
    total_messages = int(audit["total"] or 0)
    lines = [
        f"User review - {display_name(target)}",
        f"User ID: {target['user_id']}",
        f"Joined: {target['joined_at'] or 'unknown'}",
        f"Last joined: {target['last_joined_at'] or 'unknown'}",
        f"Left: {target['left_at'] or 'no'}",
        f"Status: {'pending review' if target['is_blocked'] else 'active'}",
    ]
    if target["is_blocked"]:
        lines.append(f"Block reason: {target['blocked_reason'] or 'not recorded'}")
    lines.extend(
        [
            "",
            f"Current Points: {breakdown['total']} total | {breakdown['chat']} chat | "
            f"{breakdown['invite']} invite | {breakdown['adjust']} adjust",
            f"Messages: {valid_messages} valid | {total_messages - valid_messages} rejected",
            f"Invites created: {invite_stats['valid'] or 0} valid | "
            f"{invite_stats['pending'] or 0} pending | {invite_stats['total'] or 0} total",
            f"Open risk flags: {flags['n']}",
        ]
    )
    await update.message.reply_text("\n".join(lines))


async def cmd_admin_invites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    params = []
    where = ""
    if ctx.args:
        target = resolve_user(ctx.args[0])
        if not target:
            await update.message.reply_text("User not found.")
            return
        where = "WHERE i.inviter_id=? OR i.invitee_id=?"
        params = [target["user_id"], target["user_id"]]
    rows = db_read(
        "SELECT i.*,a.username AS inviter_username,b.username AS invitee_username "
        "FROM invites i LEFT JOIN users a ON i.inviter_id=a.user_id "
        "LEFT JOIN users b ON i.invitee_id=b.user_id "
        f"{where} ORDER BY i.joined_at DESC LIMIT 20",
        tuple(params),
    )
    if not rows:
        await update.message.reply_text("No invite records found.")
        return
    lines = ["Recent invite records"]
    for row in rows:
        inviter = f"@{row['inviter_username']}" if row["inviter_username"] else str(row["inviter_id"])
        invitee = f"@{row['invitee_username']}" if row["invitee_username"] else str(row["invitee_id"])
        lines.append(
            f"{inviter} -> {invitee} | {row['status']} | +{row['awarded_points']} | "
            f"{row['joined_at'][:16] if row['joined_at'] else 'unknown'}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_admin_risks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    params = []
    where = "WHERE r.status='open'"
    if ctx.args:
        target = resolve_user(ctx.args[0])
        if not target:
            await update.message.reply_text("User not found.")
            return
        where += " AND r.user_id=?"
        params.append(target["user_id"])
    rows = db_read(
        "SELECT r.*,u.username,u.first_name,u.last_name FROM risk_flags r "
        "LEFT JOIN users u ON r.user_id=u.user_id "
        f"{where} ORDER BY r.created_at DESC LIMIT 30",
        tuple(params),
    )
    if not rows:
        await update.message.reply_text("No open risk flags.")
        return
    lines = ["Open risk flags"]
    for row in rows:
        lines.append(
            f"{row['flag_id'][:8]} | {display_name(row)} | {row['flag_type']} | {row['details']}"
        )
    lines.append("\nResolve: /admin_risk_resolve <flag_id or first 8 characters>")
    await update.message.reply_text("\n".join(lines))


async def cmd_admin_risk_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /admin_risk_resolve <flag_id>")
        return
    matches = db_read(
        "SELECT * FROM risk_flags WHERE flag_id LIKE ? AND status='open'",
        (ctx.args[0] + "%",),
    )
    if len(matches) != 1:
        await update.message.reply_text("Risk flag not found or ID prefix is ambiguous.")
        return
    row = matches[0]
    now = now_local().isoformat()
    db_write(
        "UPDATE risk_flags SET status='resolved',reviewed_at=?,reviewed_by=? WHERE flag_id=?",
        (now, update.effective_user.id, row["flag_id"]),
    )
    action_id = record_admin_action(
        update.effective_user.id,
        "resolve_risk_flag",
        row["user_id"],
        details=row["flag_id"],
    )
    await update.message.reply_text(f"Risk flag resolved.\nAudit: {action_id}")


async def cmd_admin_settle_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    cycle_id = ctx.args[0] if ctx.args else current_cycle()["cycle_id"]
    cycle = db_read_one("SELECT * FROM cycles WHERE cycle_id=?", (cycle_id,))
    if not cycle:
        await update.message.reply_text("Cycle not found.")
        return
    candidates, exclusions = await calculate_reward_candidates(ctx.bot, cycle_id)
    lines = [
        f"Settlement preview - {cycle_id}",
        f"Cycle status: {cycle['status']}",
        f"Reward pool: {cycle_reward_rules(cycle_id)[1]}U",
        f"Eligible: {len(candidates)} | Excluded after threshold: {len(exclusions)}",
        "",
    ]
    if not candidates:
        lines.append("No eligible users at this time.")
    for idx, candidate in enumerate(candidates[:30], start=1):
        lines.append(
            f"{idx}. {candidate['name']} | {candidate['points']} Points | "
            f"{candidate['reward']}U"
        )
    if len(candidates) > 30:
        lines.append(f"...and {len(candidates) - 30} more")
    if exclusions:
        lines.append("")
        lines.append("Excluded: " + ", ".join(f"{item['name']} ({item['reason']})" for item in exclusions[:15]))
    lines.append("\nPreview only. Nothing was published or changed.")
    await update.message.reply_text("\n".join(lines))


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
    if not await require_admin(update):
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
    if not await require_admin(update):
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
    if not await require_admin(update):
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
    if not await require_admin(update):
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

    cycle_row = db_read_one("SELECT status FROM cycles WHERE cycle_id=?", (cycle_id,))
    if not cycle_row:
        await update.message.reply_text("Cycle not found.")
        return
    if cycle_row["status"] != "settled":
        await update.message.reply_text(
            "Rewards can only be published after the cycle has been settled."
        )
        return

    published = db_read_one(
        "SELECT reward_id FROM rewards WHERE cycle_id=? AND status='published' LIMIT 1",
        (cycle_id,),
    )
    if published:
        await update.message.reply_text("Rewards for this cycle have already been published.")
        return

    candidates, _ = await replace_pending_rewards(ctx.bot, cycle_id)
    if not candidates:
        await update.message.reply_text("No eligible users remain after the final review check.")
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
    if not GROUP_CHAT_ID:
        await update.message.reply_text("GROUP_CHAT_ID is not configured. Rewards were not published.")
        return

    await ctx.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
    published_at = now_local().isoformat()
    db_write(
        "UPDATE rewards SET status='published', published_at=? WHERE cycle_id=?",
        (published_at, cycle_id),
    )
    action_id = record_admin_action(
        update.effective_user.id,
        "publish_rewards",
        cycle_id=cycle_id,
        details=f"published_users={len(rows)}",
    )
    await update.message.reply_text(
        f"Published rewards for {cycle_id}.\nAudit: {action_id}"
    )


# ----------------------------------------------------------------------------
# Settlement
# ----------------------------------------------------------------------------
async def calculate_reward_candidates(bot, cycle_id):
    threshold, pool = cycle_reward_rules(cycle_id)
    rows = db_read(
        "SELECT u.user_id,u.username,u.first_name,u.last_name,u.is_blocked,"
        "COALESCE(SUM(p.points),0) AS pts "
        "FROM users u JOIN point_events p ON u.user_id=p.user_id "
        "WHERE p.cycle_id=? GROUP BY u.user_id HAVING pts>=? "
        "ORDER BY pts DESC,u.user_id ASC",
        (cycle_id, threshold),
    )
    eligible_rows = []
    exclusions = []
    for row in rows:
        name = display_name(row)
        if row["is_blocked"]:
            exclusions.append({"user_id": row["user_id"], "name": name, "reason": "pending review"})
            continue
        if not await is_group_member(bot, row["user_id"]):
            exclusions.append({"user_id": row["user_id"], "name": name, "reason": "not in group"})
            continue
        eligible_rows.append(row)

    total_points = sum(int(row["pts"]) for row in eligible_rows)
    candidates = []
    for row in eligible_rows:
        weekly_points = int(row["pts"])
        amount = Decimal("0.0")
        if total_points:
            amount = (
                Decimal(weekly_points) / Decimal(total_points) * pool
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        candidates.append(
            {
                "user_id": row["user_id"],
                "name": display_name(row),
                "points": weekly_points,
                "reward": str(amount),
            }
        )
    return candidates, exclusions


async def replace_pending_rewards(bot, cycle_id):
    candidates, exclusions = await calculate_reward_candidates(bot, cycle_id)
    db_write("DELETE FROM rewards WHERE cycle_id=? AND status='pending_review'", (cycle_id,))
    if candidates:
        created_at = now_local().isoformat()
        db_many(
            "INSERT OR REPLACE INTO rewards "
            "(reward_id,cycle_id,user_id,weekly_points,reward_amount,status,created_at) "
            "VALUES (?,?,?,?,?,'pending_review',?)",
            [
                (
                    uuid.uuid4().hex,
                    cycle_id,
                    row["user_id"],
                    row["points"],
                    row["reward"],
                    created_at,
                )
                for row in candidates
            ],
        )
    return candidates, exclusions


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
    candidates, _ = await replace_pending_rewards(ctx.bot, cycle_id)
    db_write(
        "UPDATE cycles SET status='settled', settled_at=? WHERE cycle_id=?",
        (now_local().isoformat(), cycle_id),
    )
    log.info("Settled cycle %s with %s eligible reward(s)", cycle_id, len(candidates))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def user_bot_commands():
    return [
        BotCommand("perpvia", "Open PerpVia Points"),
        BotCommand("points", "View your weekly points"),
        BotCommand("rank", "View the weekly leaderboard"),
        BotCommand("invite", "Get your personal invite link"),
        BotCommand("rules", "Read the activity rules"),
    ]


def admin_bot_commands():
    return user_bot_commands() + [
        BotCommand("admin_whoami", "Check your admin access"),
        BotCommand("admin_help", "View all admin commands"),
        BotCommand("admin_adjust", "Manually adjust user Points"),
        BotCommand("admin_set_perpvia", "Configure the PerpVia reply"),
        BotCommand("admin_set_rules", "Configure the rules reply"),
        BotCommand("admin_export_points", "Export point reports"),
        BotCommand("admin_export_invites", "Export invite records"),
        BotCommand("admin_export_rewards", "Export reward records"),
        BotCommand("admin_publish_rewards", "Publish reviewed rewards"),
        BotCommand("admin_settle_preview", "Preview weekly settlement"),
        BotCommand("admin_cycle", "View cycle status"),
        BotCommand("admin_stats", "View operations statistics"),
        BotCommand("admin_user", "Review a user"),
        BotCommand("admin_invites", "Review recent invites"),
        BotCommand("admin_config", "View or change configuration"),
        BotCommand("admin_block", "Exclude a user from rewards"),
        BotCommand("admin_unblock", "Restore reward eligibility"),
        BotCommand("admin_risks", "View open risk flags"),
        BotCommand("admin_risk_resolve", "Resolve a risk flag"),
    ]


async def post_init(app):
    bot = app.bot
    users = user_bot_commands()
    admins = admin_bot_commands()
    await bot.delete_my_commands()
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(users, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(users, scope=BotCommandScopeChat(chat_id=GROUP_CHAT_ID))

    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admins,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:
            log.warning("Could not set private admin command menu for user=%s: %s", admin_id, exc)
        try:
            await bot.set_my_commands(
                admins,
                scope=BotCommandScopeChatMember(
                    chat_id=GROUP_CHAT_ID,
                    user_id=admin_id,
                ),
            )
        except Exception as exc:
            log.warning("Could not set group admin command menu for user=%s: %s", admin_id, exc)

    log.info(
        "Command menus configured for %s admin(s) and group %s",
        len(ADMIN_IDS),
        GROUP_CHAT_ID,
    )


async def on_error(update, ctx):
    update_id = getattr(update, "update_id", None)
    error = ctx.error
    log.error(
        "Unhandled Telegram update error update_id=%s",
        update_id,
        exc_info=(type(error), error, error.__traceback__),
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN")
    if not ADMIN_IDS:
        raise SystemExit("Missing ADMIN_IDS")
    if not GROUP_CHAT_ID:
        raise SystemExit("Missing GROUP_CHAT_ID")
    default_errors = []
    for key, value in DEFAULT_SETTINGS.items():
        valid, message, _ = validate_setting_value(key, value)
        if not valid:
            default_errors.append(message)
    if not default_errors:
        if int(DEFAULT_SETTINGS["chat_points_per_message"]) > int(
            DEFAULT_SETTINGS["daily_chat_points_cap"]
        ):
            default_errors.append(
                "chat_points_per_message cannot exceed daily_chat_points_cap."
            )
        if int(DEFAULT_SETTINGS["invite_points"]) > int(
            DEFAULT_SETTINGS["daily_invite_points_cap"]
        ):
            default_errors.append("invite_points cannot exceed daily_invite_points_cap.")
    if default_errors:
        raise SystemExit("Invalid environment configuration: " + " ".join(default_errors))
    init_db()
    setting_errors = validate_all_settings()
    if setting_errors:
        raise SystemExit("Invalid configuration: " + " ".join(setting_errors))
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("perpvia", cmd_perpvia))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("points", cmd_points))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("rank", cmd_rank))

    app.add_handler(CommandHandler("admin_adjust", cmd_admin_adjust))
    app.add_handler(CommandHandler("admin_whoami", cmd_admin_whoami))
    app.add_handler(CommandHandler("admin_help", cmd_admin_help))
    app.add_handler(CommandHandler("admin_set_perpvia", cmd_admin_set_perpvia))
    app.add_handler(CommandHandler("admin_set_rules", cmd_admin_set_rules))
    app.add_handler(CommandHandler("admin_export_points", cmd_admin_export_points))
    app.add_handler(CommandHandler("admin_export_invites", cmd_admin_export_invites))
    app.add_handler(CommandHandler("admin_export_rewards", cmd_admin_export_rewards))
    app.add_handler(CommandHandler("admin_publish_rewards", cmd_admin_publish_rewards))
    app.add_handler(CommandHandler("admin_config", cmd_admin_config))
    app.add_handler(CommandHandler("admin_block", cmd_admin_block))
    app.add_handler(CommandHandler("admin_unblock", cmd_admin_unblock))
    app.add_handler(CommandHandler("admin_cycle", cmd_admin_cycle))
    app.add_handler(CommandHandler("admin_stats", cmd_admin_stats))
    app.add_handler(CommandHandler("admin_user", cmd_admin_user))
    app.add_handler(CommandHandler("admin_invites", cmd_admin_invites))
    app.add_handler(CommandHandler("admin_risks", cmd_admin_risks))
    app.add_handler(CommandHandler("admin_risk_resolve", cmd_admin_risk_resolve))
    app.add_handler(CommandHandler("admin_settle_preview", cmd_admin_settle_preview))

    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_repeating(settle_due_cycles, interval=3600, first=60)
    app.job_queue.run_repeating(expire_pending_invites, interval=3600, first=120)
    app.add_error_handler(on_error)

    log.info("PerpVia PRD V1.0 bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
