"""Catbird: a bidirectional GroupMe <-> Discord bridge.

Each bridged GroupMe conversation is mirrored into exactly one Discord destination
and vice versa. A conversation is either a group's main chat or one of its topics
(subgroups), and the destination is either a text channel or a forum post/thread.
Bridges are created, changed, and removed with the `/bridge` slash command and
persisted in SQLite.

Message flow:
  Discord -> GroupMe  main-chat bridges post through the group's GroupMe bot;
                      topic bridges, and groups that refuse bot creation, post
                      with the access token (`send_as_user`).
  GroupMe -> Discord  messages are posted through a channel webhook so the
                      original sender name and avatar are preserved. Main chats
                      arrive by bot callback or polling; topics are always polled.

Every `/bridge` command except `add` and `topics` acts on the channel or post it
was run in, so no command takes a Discord channel argument.

Deletions are mirrored in both directions. GroupMe's public docs list only index
and create, but its own clients delete through the `conversations` endpoint, which
is what `groupme_delete_message` uses. GroupMe never reports a deletion, so the
poller compares the newest page of each bridged conversation with the stored
links. Edits are still not mirrored.
"""
import asyncio
import inspect
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

try:  # optional: shrink oversized images before they reach GroupMe
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow ships in requirements.txt
    Image = None

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GROUPME_ACCESS_TOKEN = os.environ.get("GROUPME_ACCESS_TOKEN", "").strip()
GROUPME_MODE = (os.environ.get("GROUPME_MODE", "poll").strip().lower() or "poll")
GROUPME_API_BASE = os.environ.get("GROUPME_API_BASE", "https://api.groupme.com/v3").rstrip("/")
GROUPME_IMAGE_API = os.environ.get("GROUPME_IMAGE_API", "https://image.groupme.com").rstrip("/")
GROUPME_POLL_SECONDS = max(5, int(os.environ.get("GROUPME_POLL_SECONDS", "10")))
# Most poll cycles look at only the newest few messages; every
# GROUPME_POLL_BACKFILL_SECONDS the same loop asks for the wider window instead.
GROUPME_POLL_RECENT_LIMIT = max(
    1, min(100, int(os.environ.get("GROUPME_POLL_RECENT_LIMIT", "5")))
)
GROUPME_POLL_BACKFILL_SECONDS = max(
    GROUPME_POLL_SECONDS, int(os.environ.get("GROUPME_POLL_BACKFILL_SECONDS", "300"))
)
GROUPME_POLL_BACKFILL_EVERY = max(
    1, round(GROUPME_POLL_BACKFILL_SECONDS / GROUPME_POLL_SECONDS)
)
# Widest window a poll fetches, and the window used to detect deleted messages.
GROUPME_HISTORY_LIMIT = max(
    GROUPME_POLL_RECENT_LIMIT,
    min(100, int(os.environ.get("GROUPME_HISTORY_LIMIT", "25"))),
)
GROUPME_REQUEST_DELAY_SECONDS = float(os.environ.get("GROUPME_REQUEST_DELAY_SECONDS", "1"))
# Mirror deletions: a deleted Discord message removes its GroupMe copy, and a
# GroupMe message that disappears takes its mirrored Discord copy with it.
SYNC_DELETIONS = os.environ.get("SYNC_DELETIONS", "true").strip().lower() not in {"0", "false", "no", "off"}
# GroupMe keeps deleted messages as tombstones and emits a `message.deleted`
# system event; this interval is the fallback check for links whose event was missed.
GROUPME_DELETE_CHECK_SECONDS = max(60, int(os.environ.get("GROUPME_DELETE_CHECK_SECONDS", "60")))
# Loopback by default: the ngrok tunnel forwards to localhost, so nothing has to be
# reachable from the internet (the callback path has no authentication of its own).
# Set GROUPME_WEBHOOK_HOST=0.0.0.0 only when a proxy on another host needs to connect.
GROUPME_WEBHOOK_HOST = os.environ.get("GROUPME_WEBHOOK_HOST", "127.0.0.1")
GROUPME_WEBHOOK_PORT = int(os.environ.get("GROUPME_WEBHOOK_PORT", "8085"))
GROUPME_WEBHOOK_PATH = os.environ.get("GROUPME_WEBHOOK_PATH", "/groupme/callback")
GROUPME_PUBLIC_URL = os.environ.get("GROUPME_PUBLIC_URL", "").rstrip("/")
# The ngrok SDK reads NGROK_AUTHTOKEN from the environment when authtoken_from_env is set.
NGROK_ENABLED = os.environ.get("NGROK_ENABLED", "false").strip().lower() not in {"0", "false", "no", "off"}
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "catbird.db")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
VERBOSE_LOGGING = os.environ.get("VERBOSE_LOGGING", "false").strip().lower() not in {"0", "false", "no", "off"}
# Dump every raw GroupMe callback to the log and payload log so undocumented fields
# (for example GroupMe topics) can be inspected.
LOG_GROUPME_PAYLOADS = os.environ.get("LOG_GROUPME_PAYLOADS", "false").strip().lower() not in {"0", "false", "no", "off"}
GROUPME_PAYLOAD_LOG = os.environ.get("GROUPME_PAYLOAD_LOG", "groupme_payloads.jsonl")

# Base URL GroupMe can reach the callback server at. Replaced by the tunnel URL at startup.
PUBLIC_BASE_URL = GROUPME_PUBLIC_URL

MAX_RETRIES = 5
# 2: bridges key on the GroupMe conversation and record the Discord target kind
# (plain channel vs forum post/thread). Changing this rebuilds the tables, so the
# bridges have to be recreated with `/bridge add`.
SCHEMA_VERSION = 2
GROUPME_TEXT_LIMIT = 1000
MAX_GROUPME_IMAGES = 10
MAX_DISCORD_FILES = 10
DISCORD_FILE_LIMIT_BYTES = 8 * 1024 * 1024
# Images are re-encoded down to this budget before being uploaded to GroupMe's
# image service, and are downscaled to IMAGE_MAX_DIMENSION on the long edge.
IMAGE_UPLOAD_LIMIT_BYTES = int(os.environ.get("IMAGE_UPLOAD_LIMIT_BYTES", str(8 * 1024 * 1024)))
IMAGE_MAX_DIMENSION = max(256, int(os.environ.get("IMAGE_MAX_DIMENSION", "2048")))
IMAGE_JPEG_QUALITY = min(95, max(50, int(os.environ.get("IMAGE_JPEG_QUALITY", "85"))))
# Originals are read up to this size so they can be compressed; bigger ones are dropped.
IMAGE_SOURCE_LIMIT_BYTES = max(IMAGE_UPLOAD_LIMIT_BYTES * 8, 32 * 1024 * 1024)
WEBHOOK_NAME = "GroupMe Bridge"
GROUPME_BOT_NAME = os.environ.get("GROUPME_BOT_NAME", "Catbird Bridge").strip() or "Catbird Bridge"
# Local image file (relative to bot.py) used as the bot avatar; falls back to this URL.
GROUPME_BOT_AVATAR_PATH = os.environ.get("GROUPME_BOT_AVATAR_PATH", "").strip()
GROUPME_BOT_AVATAR_URL = os.environ.get("GROUPME_BOT_AVATAR_URL", "").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LINK_GROUPME = "groupme_to_discord"
# Bridge-wide policy for GroupMe display names. Keyed by the display name, never
# the user id, so two users sharing a name share the setting.
PRIVACY_SHARE = "share"
PRIVACY_ANONYMIZE = "anonymize"
PRIVACY_OPTOUT = "optout"
ANONYMOUS_GROUPME_NAME = "GroupMe user"
IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

REQUIRED_PERMS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    manage_webhooks=True,
    # Fallback for deleting a mirrored message when its webhook was replaced.
    manage_messages=True,
)

log = logging.getLogger("catbird")

# GroupMe message ids this process posted into a topic. Topic posts are made with
# the access token (sender_type "user"), so they must be suppressed explicitly to
# avoid echoing them straight back into Discord.
RECENT_OUTBOUND: dict[str, float] = {}

# Names of GroupMe bots owned by the configured account. GroupMe bot messages are
# normally rejected by `sender_type`, but a bot can also be spotted by name when
# that field is absent from a payload.
BOT_NAMES: set[str] = set()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def from_epoch(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return utcnow()


def append_payload_log(payload: dict) -> None:
    """Append a raw GroupMe payload to disk so undocumented fields can be inspected."""
    try:
        with open(GROUPME_PAYLOAD_LOG, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"logged_at": iso(utcnow()), "payload": payload}, ensure_ascii=False)
                + "\n"
            )
    except OSError:
        log.exception("Could not append to %s", GROUPME_PAYLOAD_LOG)


# --------------------------------------------------------------------------- #
# Local state
# --------------------------------------------------------------------------- #

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

CREATE_SQL = f"""
    -- Exactly one GroupMe conversation <-> one Discord destination.
    -- conversation_id is a group id for main-chat bridges and a subgroup (topic) id
    -- for topic bridges; group_id always points at the parent group.
    CREATE TABLE IF NOT EXISTS bridges (
        conversation_id TEXT PRIMARY KEY,
        conversation_type TEXT NOT NULL DEFAULT 'group',
        group_id TEXT NOT NULL,
        group_name TEXT NOT NULL DEFAULT '',
        conversation_name TEXT NOT NULL DEFAULT '',
        channel_id INTEGER NOT NULL UNIQUE,
        target_kind TEXT NOT NULL DEFAULT 'channel',
        parent_id INTEGER,
        bot_id TEXT NOT NULL DEFAULT '',
        webhook_id INTEGER,
        webhook_token TEXT,
        send_mode TEXT NOT NULL DEFAULT 'bot',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS bridges_group ON bridges (group_id);
    -- Last GroupMe message id relayed (or baselined) per conversation id.
    CREATE TABLE IF NOT EXISTS group_state (
        group_id TEXT PRIMARY KEY,
        last_message_id TEXT
    );
    -- GroupMe source message -> Discord message mapping, used for de-duplication.
    CREATE TABLE IF NOT EXISTS message_links (
        direction TEXT NOT NULL,
        group_id TEXT NOT NULL,
        channel_id INTEGER,
        source_id TEXT NOT NULL,
        target_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (direction, group_id, source_id)
    );
    CREATE INDEX IF NOT EXISTS message_links_source ON message_links (direction, source_id);
    -- Discord message -> GroupMe message(s) posted by the bridge, for deletion sync.
    -- One Discord message can become several GroupMe messages: the text post plus
    -- one picture post per image, because /bots/post returns no message id.
    CREATE TABLE IF NOT EXISTS outbound_messages (
        group_id TEXT NOT NULL,
        discord_id TEXT NOT NULL,
        groupme_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (group_id, discord_id, groupme_id)
    );
    CREATE INDEX IF NOT EXISTS outbound_messages_discord ON outbound_messages (discord_id);
    -- Bridge-wide GroupMe display-name policy for messages mirrored into Discord.
    -- Keyed by the display name (casefolded), not the user id; a renamed GroupMe
    -- user needs a new entry. Kept out of DROP_SQL so it survives a schema rebuild.
    CREATE TABLE IF NOT EXISTS groupme_user_privacy (
        name_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        mode TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    PRAGMA user_version = {SCHEMA_VERSION};
"""

DROP_SQL = """
    DROP TABLE IF EXISTS bridges;
    DROP TABLE IF EXISTS bridges_v1;
    DROP TABLE IF EXISTS group_state;
    DROP TABLE IF EXISTS message_links;
    DROP TABLE IF EXISTS outbound_messages;
"""


def init_db() -> None:
    """Create the schema. A database from any other version is rebuilt from scratch."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        log.warning(
            "Schema version %s (expected %s); recreating the bridge tables",
            version,
            SCHEMA_VERSION,
        )
        db.executescript(DROP_SQL)
    db.executescript(CREATE_SQL)
    db.commit()


init_db()


def bridge_for_conversation(conversation_id: str) -> sqlite3.Row | None:
    """The bridge for a GroupMe conversation: a main-chat (group) id or a topic id."""
    return db.execute(
        "SELECT * FROM bridges WHERE conversation_id=?", (str(conversation_id),)
    ).fetchone()


def bridges_for_group(group_id: str) -> list[sqlite3.Row]:
    """Every bridge fed by a GroupMe group: its main chat and any of its topics."""
    return db.execute(
        "SELECT * FROM bridges WHERE group_id=? ORDER BY conversation_type DESC",
        (str(group_id),),
    ).fetchall()


def bridge_for_destination(channel_id: int) -> sqlite3.Row | None:
    """The bridge that writes into a Discord channel, forum post, or thread."""
    return db.execute(
        "SELECT * FROM bridges WHERE channel_id=?", (int(channel_id),)
    ).fetchone()


def list_bridges() -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM bridges ORDER BY group_name COLLATE NOCASE, conversation_name COLLATE NOCASE"
    ).fetchall()


def outbound_conversation_id(bridge: sqlite3.Row) -> str:
    """Where Discord messages for this bridge are posted."""
    return str(bridge["conversation_id"])


def webhook_channel_id(bridge: sqlite3.Row) -> int:
    """Channel that owns the bridge webhook: the parent, for forum post/thread targets."""
    parent_id = bridge["parent_id"]
    if bridge["target_kind"] == "thread" and parent_id:
        return int(parent_id)
    return int(bridge["channel_id"])


def conversation_label(bridge: sqlite3.Row) -> str:
    """`GroupName`, or `#topic` for a topic bridge."""
    name = bridge["conversation_name"] or bridge["conversation_id"]
    if bridge["conversation_type"] == "topic":
        return f"#{bridge['conversation_name'] or bridge['conversation_id']}"
    return bridge["group_name"] or name


def destination_label(bridge: sqlite3.Row) -> str:
    label = f"<#{bridge['channel_id']}>"
    if bridge["target_kind"] == "thread":
        label += " (post)"
    return label


def save_bridge(
    conversation_id: str,
    channel_id: int,
    bot_id: str,
    conversation_name: str,
    *,
    conversation_type: str = "group",
    group_id: str = "",
    group_name: str = "",
    target_kind: str = "channel",
    parent_id: int | None = None,
    webhook_id: int | None = None,
    webhook_token: str | None = None,
    send_mode: str = "bot",
) -> None:
    db.execute(
        """
        INSERT INTO bridges
            (conversation_id, conversation_type, group_id, group_name, conversation_name,
             channel_id, target_kind, parent_id, bot_id, webhook_id, webhook_token,
             send_mode, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            conversation_type=excluded.conversation_type,
            group_id=excluded.group_id,
            group_name=excluded.group_name,
            conversation_name=excluded.conversation_name,
            channel_id=excluded.channel_id,
            target_kind=excluded.target_kind,
            parent_id=excluded.parent_id,
            bot_id=excluded.bot_id,
            webhook_id=COALESCE(excluded.webhook_id, bridges.webhook_id),
            webhook_token=COALESCE(excluded.webhook_token, bridges.webhook_token),
            send_mode=excluded.send_mode
        """,
        (
            str(conversation_id),
            conversation_type,
            str(group_id or conversation_id),
            group_name,
            conversation_name,
            int(channel_id),
            target_kind,
            int(parent_id) if parent_id else None,
            bot_id,
            webhook_id,
            webhook_token,
            send_mode,
            iso(utcnow()),
        ),
    )
    db.commit()


def set_bridge_bot(conversation_id: str, bot_id: str) -> None:
    db.execute(
        "UPDATE bridges SET bot_id=? WHERE conversation_id=?",
        (bot_id, str(conversation_id)),
    )
    db.commit()


def set_bridge_send_mode(conversation_id: str, send_mode: str) -> None:
    """Choose the posting path without touching the bot used for callbacks."""
    db.execute(
        "UPDATE bridges SET send_mode=? WHERE conversation_id=?",
        (send_mode, str(conversation_id)),
    )
    db.commit()


def update_bridge_conversation(
    old_conversation_id: str,
    conversation_id: str,
    conversation_type: str,
    conversation_name: str,
    send_mode: str,
    *,
    group_id: str = "",
    group_name: str = "",
    bot_id: str | None = None,
) -> None:
    """Point an existing bridge (same Discord destination) at another conversation."""
    if bot_id is None:
        db.execute(
            """
            UPDATE bridges
               SET conversation_id=?, conversation_type=?, conversation_name=?, send_mode=?,
                   group_id=COALESCE(NULLIF(?, ''), group_id),
                   group_name=COALESCE(NULLIF(?, ''), group_name)
             WHERE conversation_id=?
            """,
            (
                str(conversation_id),
                conversation_type,
                conversation_name,
                send_mode,
                str(group_id),
                group_name,
                str(old_conversation_id),
            ),
        )
    else:
        db.execute(
            """
            UPDATE bridges
               SET conversation_id=?, conversation_type=?, conversation_name=?, send_mode=?,
                   bot_id=?,
                   group_id=COALESCE(NULLIF(?, ''), group_id),
                   group_name=COALESCE(NULLIF(?, ''), group_name)
             WHERE conversation_id=?
            """,
            (
                str(conversation_id),
                conversation_type,
                conversation_name,
                send_mode,
                bot_id,
                str(group_id),
                group_name,
                str(old_conversation_id),
            ),
        )
    db.commit()


def save_webhook(conversation_id: str, webhook_id: int, webhook_token: str) -> None:
    db.execute(
        "UPDATE bridges SET webhook_id=?, webhook_token=? WHERE conversation_id=?",
        (int(webhook_id), webhook_token, str(conversation_id)),
    )
    db.commit()


def delete_bridge(conversation_id: str) -> None:
    db.execute("DELETE FROM bridges WHERE conversation_id=?", (str(conversation_id),))
    db.execute("DELETE FROM group_state WHERE group_id=?", (str(conversation_id),))
    db.execute("DELETE FROM outbound_messages WHERE group_id=?", (str(conversation_id),))
    db.execute("DELETE FROM message_links WHERE group_id=?", (str(conversation_id),))
    db.commit()


def last_seen(group_id: str) -> str | None:
    row = db.execute(
        "SELECT last_message_id FROM group_state WHERE group_id=?", (str(group_id),)
    ).fetchone()
    return row[0] if row and row[0] else None


def set_last_seen(group_id: str, message_id: str) -> None:
    db.execute(
        """
        INSERT INTO group_state (group_id, last_message_id) VALUES (?,?)
        ON CONFLICT(group_id) DO UPDATE SET last_message_id=excluded.last_message_id
        """,
        (str(group_id), str(message_id)),
    )
    db.commit()


def add_link(
    direction: str,
    group_id: str,
    channel_id: int | None,
    source_id: str,
    target_id: str | None,
    created_at: datetime,
) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO message_links
            (direction, group_id, channel_id, source_id, target_id, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (direction, str(group_id), channel_id, str(source_id), target_id, iso(created_at)),
    )
    db.commit()


def link_exists(direction: str, group_id: str, source_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM message_links WHERE direction=? AND group_id=? AND source_id=?",
            (direction, str(group_id), str(source_id)),
        ).fetchone()
        is not None
    )


def remove_link(direction: str, group_id: str, source_id: str) -> None:
    db.execute(
        "DELETE FROM message_links WHERE direction=? AND group_id=? AND source_id=?",
        (direction, str(group_id), str(source_id)),
    )
    db.commit()


def add_outbound(
    group_id: str, discord_id: str | int, groupme_id: str, created_at: datetime
) -> None:
    """Remember which GroupMe message(s) a Discord message became."""
    db.execute(
        """
        INSERT OR REPLACE INTO outbound_messages
            (group_id, discord_id, groupme_id, created_at)
        VALUES (?,?,?,?)
        """,
        (str(group_id), str(discord_id), str(groupme_id), iso(created_at)),
    )
    db.commit()


def outbound_messages(discord_id: str | int) -> list[sqlite3.Row]:
    """GroupMe messages that were posted for a Discord message, across conversations."""
    return db.execute(
        "SELECT * FROM outbound_messages WHERE discord_id=? ORDER BY created_at",
        (str(discord_id),),
    ).fetchall()


def remove_outbound(group_id: str, discord_id: str | int, groupme_id: str) -> None:
    db.execute(
        "DELETE FROM outbound_messages WHERE group_id=? AND discord_id=? AND groupme_id=?",
        (str(group_id), str(discord_id), str(groupme_id)),
    )
    db.commit()


def outbound_groupme_id_exists(group_id: str, groupme_id: str) -> bool:
    """Whether a GroupMe message is already attributed to some Discord message."""
    return (
        db.execute(
            "SELECT 1 FROM outbound_messages WHERE group_id=? AND groupme_id=?",
            (str(group_id), str(groupme_id)),
        ).fetchone()
        is not None
    )


def normalize_groupme_name(name: str) -> str:
    """Match a display name case-insensitively, ignoring repeated whitespace."""
    return " ".join((name or "").split()).casefold()


def clear_groupme_user_mode(name: str) -> None:
    db.execute(
        "DELETE FROM groupme_user_privacy WHERE name_key=?",
        (normalize_groupme_name(name),),
    )
    db.commit()


def set_groupme_user_mode(name: str, mode: str) -> None:
    """Store a bridge-wide policy for a GroupMe display name."""
    if mode not in {PRIVACY_SHARE, PRIVACY_ANONYMIZE, PRIVACY_OPTOUT}:
        raise ValueError(f"unknown privacy mode {mode!r}")
    display = " ".join((name or "").split())
    if not display:
        return
    if mode == PRIVACY_SHARE:
        clear_groupme_user_mode(display)
        return
    db.execute(
        """
        INSERT INTO groupme_user_privacy (name_key, name, mode, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(name_key) DO UPDATE SET
            name=excluded.name,
            mode=excluded.mode,
            updated_at=excluded.updated_at
        """,
        (display.casefold(), display, mode, iso(utcnow())),
    )
    db.commit()


def groupme_user_mode(name: str) -> str:
    """`optout`, `anonymize`, or "" for a GroupMe display name."""
    key = normalize_groupme_name(name)
    if not key:
        return ""
    row = db.execute(
        "SELECT mode FROM groupme_user_privacy WHERE name_key=?", (key,)
    ).fetchone()
    return str(row["mode"]) if row else ""


def list_groupme_user_modes() -> list[sqlite3.Row]:
    """Every display-name policy, ordered for the /privacy list command."""
    return db.execute(
        "SELECT * FROM groupme_user_privacy ORDER BY mode, name COLLATE NOCASE"
    ).fetchall()


# --------------------------------------------------------------------------- #
# GroupMe API
# --------------------------------------------------------------------------- #


class GroupMeError(Exception):
    """A GroupMe API call failed or exhausted its retries."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def groupme_headers() -> dict[str, str]:
    """Auth header only: aiohttp picks the content type from `json=` / `data=`."""
    headers: dict[str, str] = {}
    if GROUPME_ACCESS_TOKEN:
        headers["X-Access-Token"] = GROUPME_ACCESS_TOKEN
    return headers


async def groupme_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    form: dict | None = None,
):
    """Call the GroupMe API, retrying rate limits and server errors."""
    url = f"{GROUPME_API_BASE}{path}"
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                data=form,
                headers=groupme_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                # GroupMe answers 304 when a pagination cursor has no new messages.
                if response.status == 304:
                    return None
                body = await response.text()
                if response.status == 429 or response.status >= 500:
                    delay = attempt * 2.0
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    log.warning(
                        "GroupMe %s %s returned %s; retrying in %.1fs (%d/%d)",
                        method, path, response.status, delay, attempt, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status >= 400:
                    raise GroupMeError(
                        f"{method} {path} failed with {response.status}: {body[:300]}",
                        response.status,
                    )
                if not body.strip():
                    return None
                payload = json.loads(body)
                return payload.get("response")
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            last_error = error
            if attempt == MAX_RETRIES:
                break
            await asyncio.sleep(attempt)
    raise GroupMeError(f"{method} {path} failed after {MAX_RETRIES} attempts: {last_error}")


async def groupme_get_group(session: aiohttp.ClientSession, group_id: str) -> dict | None:
    return await groupme_request(
        session, "GET", f"/groups/{group_id}", params={"omit": "memberships"}
    )


async def groupme_owns_group(session: aiohttp.ClientSession, group_id: str) -> bool | None:
    """Whether `group_id` is a group the access token is a member of.

    Used to tell "this id is a topic, not a group" apart from "the token is bad":
    `GET /groups/{id}` accepts a subgroup id, but `POST /bots` answers 401 for one
    because topics cannot host bots. Returns None when that cannot be determined.
    """
    for page in range(1, 6):
        try:
            groups = await groupme_request(
                session,
                "GET",
                "/groups",
                params={"page": page, "per_page": 100, "omit": "memberships"},
            )
        except GroupMeError:
            return None
        if not isinstance(groups, list):
            return None
        if any(str(group.get("id")) == str(group_id) for group in groups):
            return True
        if len(groups) < 100:
            return False
    return None


async def groupme_list_messages(
    session: aiohttp.ClientSession,
    group_id: str,
    *,
    since_id: str | None = None,
    limit: int = GROUPME_HISTORY_LIMIT,
) -> list[dict]:
    params = {"limit": limit}
    if since_id:
        params["since_id"] = since_id
    response = await groupme_request(session, "GET", f"/groups/{group_id}/messages", params=params)
    if not response:
        return []
    return response.get("messages") or []


async def groupme_delete_message(
    session: aiohttp.ClientSession, conversation_id: str, message_id: str
) -> None:
    """Delete a message the token owner is allowed to remove.

    GroupMe documents only index and create, but its own clients delete through
    `DELETE /conversations/:conversation_id/messages/:message_id` (204). A 404
    means the message is already gone, which is the desired end state.
    """
    try:
        await groupme_request(
            session,
            "DELETE",
            f"/conversations/{conversation_id}/messages/{message_id}",
        )
    except GroupMeError as error:
        if error.status != 404:
            raise


async def groupme_list_bots(session: aiohttp.ClientSession) -> list[dict]:
    return await groupme_request(session, "GET", "/bots") or []


async def groupme_create_bot(
    session: aiohttp.ClientSession,
    group_id: str,
    name: str,
    callback_url: str = "",
    avatar_url: str = "",
) -> dict:
    """Create a posting bot for a group. GroupMe wraps the result in {"bot": {...}}."""
    form = {
        "bot[name]": name,
        "bot[group_id]": str(group_id),
        "bot[active]": "true",
        "bot[dm_notification]": "false",
    }
    if callback_url:
        form["bot[callback_url]"] = callback_url
    if avatar_url:
        form["bot[avatar_url]"] = avatar_url
    response = await groupme_request(session, "POST", "/bots", form=form)
    if isinstance(response, dict):
        bot = response.get("bot")
        return bot if isinstance(bot, dict) else response
    return {}


async def groupme_find_bot(
    session: aiohttp.ClientSession, group_id: str, callback_url: str = ""
) -> dict | None:
    """Find an existing bot for a group, preferring one already pointed at our callback."""
    bots = await groupme_list_bots(session)
    group_bots = [bot for bot in bots if str(bot.get("group_id")) == str(group_id)]
    if callback_url:
        for bot in group_bots:
            if bot.get("callback_url") == callback_url:
                return bot
    return group_bots[0] if group_bots else None


async def groupme_destroy_bot(session: aiohttp.ClientSession, bot_id: str) -> None:
    """Remove a bot. GroupMe has no update endpoint, so avatars require this + create."""
    await groupme_request(session, "POST", "/bots/destroy", form={"bot_id": str(bot_id)})


async def groupme_create_bot_with_fallback(
    session: aiohttp.ClientSession, group_id: str, avatar_url: str = ""
) -> dict:
    """Create a bot, retrying without the avatar when GroupMe refuses it.

    GroupMe silently accepts the form but can reject `bot[avatar_url]` outright; the
    retry keeps a bad avatar from blocking the bridge. Raises the last `GroupMeError`.
    """
    attempts = [avatar_url, ""] if avatar_url else [""]
    last_error: GroupMeError | None = None
    for avatar in attempts:
        try:
            created = await groupme_create_bot(
                session, group_id, GROUPME_BOT_NAME, callback_url(), avatar
            )
        except GroupMeError as error:
            message = str(error).lower()
            if error.status == 400 and "already registered" in message:
                raise
            log.warning(
                "GroupMe rejected bot creation for group %s (avatar=%s): %s",
                group_id,
                bool(avatar),
                error,
            )
            last_error = error
            continue
        if str(created.get("bot_id") or ""):
            return created
        last_error = GroupMeError("GroupMe returned no bot id")
    raise last_error or GroupMeError("bot creation failed")


async def ensure_group_bot(
    session: aiohttp.ClientSession, group_id: str, avatar_url: str = ""
) -> tuple[str, str]:
    """Create or reuse a bot for a group. Returns (bot_id, note); id is "" on failure."""
    failure = ""
    try:
        created = await groupme_create_bot_with_fallback(session, group_id, avatar_url)
    except GroupMeError as error:
        failure = f"GroupMe rejected the bot request: `{error}`."
    else:
        bot_id = str(created.get("bot_id") or "")
        note = f"Created GroupMe bot **{GROUPME_BOT_NAME}** (`{bot_id}`)."
        if avatar_url and not created.get("avatar_url"):
            note += " GroupMe ignored the bot avatar."
        return bot_id, note

    # Creation failed: a bot may already exist for this group (same callback, a
    # different app, or a per-group limit), and reusing it still beats no bridge.
    try:
        existing = await groupme_find_bot(session, group_id, callback_url())
    except GroupMeError as error:
        return "", f"{failure} Listing the account's bots also failed: `{error}`."
    bot_id = str((existing or {}).get("bot_id") or "")
    if not bot_id:
        return "", f"{failure} No existing bot for this group could be listed either."

    note = f"Reusing the existing GroupMe bot `{bot_id}` for this group ({failure})"
    if callback_url() and existing.get("callback_url") and existing["callback_url"] != callback_url():
        note += (
            "\n⚠️ That bot posts to a different callback URL, so GroupMe messages may not "
            f"arrive; point it at `{callback_url()}` or run `/bridge set-bot`."
        )
    return bot_id, note


async def groupme_post_bot(
    session: aiohttp.ClientSession, bot_id: str, text: str, picture_url: str | None = None
) -> None:
    payload = {"bot_id": bot_id, "text": text}
    if picture_url:
        payload["picture_url"] = picture_url
    # GroupMe does not return the created message for bot posts.
    await groupme_request(session, "POST", "/bots/post", json_body=payload)


async def groupme_post_message(
    session: aiohttp.ClientSession,
    conversation_id: str,
    text: str,
    attachments: list[dict] | None = None,
) -> dict | None:
    """Post as the token owner (the only way to write into a topic/subgroup)."""
    message: dict = {"source_guid": uuid.uuid4().hex}
    if text:
        message["text"] = text
    if attachments:
        message["attachments"] = attachments
    response = await groupme_request(
        session,
        "POST",
        f"/groups/{conversation_id}/messages",
        json_body={"message": message},
    )
    if isinstance(response, dict):
        return response.get("message")
    return None


async def groupme_list_subgroups(
    session: aiohttp.ClientSession, group_id: str
) -> list[dict]:
    """GroupMe topics are subgroups; this endpoint is undocumented but stable."""
    try:
        return await groupme_request(session, "GET", f"/groups/{group_id}/subgroups") or []
    except GroupMeError as error:
        if error.status == 404:
            return []
        raise


async def groupme_upload_image(
    session: aiohttp.ClientSession, data: bytes, content_type: str
) -> str:
    """Push image bytes through GroupMe's image service and return the hosted URL."""
    headers = {"Content-Type": content_type or "image/jpeg", "X-Access-Token": GROUPME_ACCESS_TOKEN}
    async with session.post(
        f"{GROUPME_IMAGE_API}/pictures",
        data=data,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise GroupMeError(
                f"image upload failed with {response.status} for {len(data)} bytes: {body[:200]}",
                response.status,
            )
        payload = json.loads(body).get("payload") or {}
        url = payload.get("url") or ""
        if not url:
            raise GroupMeError("image upload returned no URL")
        return url


# --------------------------------------------------------------------------- #
# Message rendering helpers
# --------------------------------------------------------------------------- #

CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
# Emoji are dropped on the way to GroupMe: custom Discord emoji have no meaning
# there and unicode emoji are not requested by the server owner.
UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, symbols, flags, supplemental
    "\u2600-\u27bf"          # misc symbols and dingbats
    "\u2b00-\u2bff"
    "\ufe0e\ufe0f\u200d"     # variation selectors and zero-width joiner
    "]+"
)
WHITESPACE_RE = re.compile(r"[ \t]{2,}")


def truncate(text: str, limit: int = GROUPME_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_SUFFIXES)


def strip_emoji(text: str) -> str:
    return UNICODE_EMOJI_RE.sub("", text)


def render_discord_content(message: discord.Message) -> str:
    """Rewrite Discord mention syntax into text GroupMe members can read.

    Users and roles become `@Name`, channels become `#name`, and emoji are
    removed instead of being passed through as unreadable markup.
    """
    text = message.content or ""
    for user in message.mentions:
        name = user.display_name if isinstance(user, discord.Member) else user.name
        text = text.replace(f"<@{user.id}>", f"@{name}").replace(f"<@!{user.id}>", f"@{name}")
    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")
    for channel in message.channel_mentions:
        text = text.replace(f"<#{channel.id}>", f"#{channel.name}")
    text = CUSTOM_EMOJI_RE.sub("", text)
    text = USER_MENTION_RE.sub(r"@user", text)
    text = ROLE_MENTION_RE.sub(r"@role", text)
    text = CHANNEL_MENTION_RE.sub(r"#channel", text)
    text = strip_emoji(text)
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def quoted_reply(message: discord.Message) -> str:
    reference = message.reference.resolved if message.reference else None
    if not isinstance(reference, discord.Message):
        return ""
    snippet = truncate(strip_emoji(CUSTOM_EMOJI_RE.sub("", reference.clean_content or "")), 120)
    if not snippet:
        return ""
    return f"> {reference.author.display_name}: {snippet}\n"


def groupme_reply_quote(attachment: dict) -> str:
    """Render a GroupMe `reply` attachment as a Markdown blockquote.

    GroupMe sends a reply as `{"type": "reply", "reply_id", "user_id", "name", "text"}`,
    but current payloads often omit both `name` and `text`, leaving
    `> (GroupMe reply)`. When they are present, the name follows the display-name
    privacy rule: anonymized names become `GroupMe user`, and an opted-out name loses
    both the name and the quoted text.
    """
    name = (attachment.get("name") or "").strip()
    quoted = (attachment.get("text") or "").strip()
    privacy = groupme_user_mode(name)
    if privacy == PRIVACY_OPTOUT:
        return "> (replying to a GroupMe user who opted out of Discord sharing)"
    if privacy == PRIVACY_ANONYMIZE and name:
        name = ANONYMOUS_GROUPME_NAME
    quoted = truncate(strip_emoji(CUSTOM_EMOJI_RE.sub("", quoted)), 120)
    if name and quoted:
        return f"> {name}: {quoted}"
    if name:
        return f"> Replying to {name}"
    if quoted:
        return f"> {quoted}"
    return "> (GroupMe reply)"


def groupme_message_text(message: dict) -> str:
    parts: list[str] = []
    quotes: list[str] = []
    text = (message.get("text") or "").strip()
    if text:
        parts.append(text)
    for attachment in message.get("attachments") or []:
        kind = (attachment.get("type") or "").lower()
        if kind == "location":
            label = attachment.get("name") or "Location"
            lat, lng = attachment.get("lat"), attachment.get("lng")
            if lat and lng:
                parts.append(f"📍 {label}: https://maps.google.com/?q={lat},{lng}")
        elif kind == "split":
            parts.append("(GroupMe poll split)")
        elif kind == "reply":
            # Quoted first, like a Discord reply, so the context reads before the answer.
            quotes.append(groupme_reply_quote(attachment))
        elif kind not in {"image", "emoji"}:
            parts.append(f"[{kind or 'attachment'}]")
    body = "\n".join(parts).strip()
    return truncate("\n".join(quotes + ([body] if body else [])))


def groupme_image_urls(message: dict) -> list[str]:
    urls = []
    for attachment in message.get("attachments") or []:
        if (attachment.get("type") or "").lower() == "image" and attachment.get("url"):
            urls.append(attachment["url"])
    return urls


def is_groupme_bot_message(message: dict) -> bool:
    """True for system messages, bot messages, and anything without a real user."""
    if message.get("system"):
        return True
    sender_type = (message.get("sender_type") or "").strip().lower()
    if sender_type and sender_type != "user":
        return True
    if not message.get("user_id"):
        return True
    name = (message.get("name") or "").strip().casefold()
    return bool(name and name in BOT_NAMES)


def groupme_deleted_message_id(message: dict) -> str:
    """The id of a message GroupMe reported as deleted, or "".

    A deletion arrives as a `system` message whose `event.type` is
    `message.deleted` and whose `event.data.message_id` is the original id; the
    original message also stays in the index as a tombstone carrying `deleted_at`.
    The tombstone's text is not used: a real message could contain it.
    """
    event = message.get("event") or {}
    if (event.get("type") or "") == "message.deleted":
        data = event.get("data") or {}
        return str(data.get("message_id") or "")
    if message.get("deleted_at"):
        return str(message.get("id") or "")
    return ""


def sanitize_webhook_name(name: str) -> str:
    cleaned = (name or "").strip() or "GroupMe user"
    cleaned = cleaned.replace("@", "@\u200b")
    if cleaned.lower() in {"clyde", "discord"}:
        cleaned = f"{cleaned} (GroupMe)"
    return cleaned[:80]


async def read_body_limited(response: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """Read a whole response body, or None when it exceeds `limit`.

    `StreamReader.read(n)` may return fewer bytes than requested, so the body has
    to be drained in a loop; a single read truncates large images.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await response.content.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def download_groupme_images(
    session: aiohttp.ClientSession, urls: list[str]
) -> list[discord.File]:
    files: list[discord.File] = []
    for index, url in enumerate(urls[:MAX_DISCORD_FILES], start=1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    log.warning("Could not download GroupMe image %s (%s)", url, response.status)
                    continue
                data = await read_body_limited(response, DISCORD_FILE_LIMIT_BYTES)
                if data is None:
                    log.warning("Skipping GroupMe image %s: larger than %d bytes", url, DISCORD_FILE_LIMIT_BYTES)
                    continue
                extension = IMAGE_TYPES.get(response.content_type or "", "jpg")
                files.append(
                    discord.File(io.BytesIO(data), filename=f"groupme-{index}.{extension}")
                )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("Download of GroupMe image %s failed", url)
    return files


async def download_discord_image(
    session: aiohttp.ClientSession, attachment: discord.Attachment
) -> tuple[bytes, str] | None:
    """Fetch and compress a Discord attachment so it can be re-hosted on GroupMe."""
    if attachment.size and attachment.size > IMAGE_SOURCE_LIMIT_BYTES:
        log.warning(
            "Skipping Discord attachment %s: %d bytes exceeds the %d byte source cap",
            attachment.filename,
            attachment.size,
            IMAGE_SOURCE_LIMIT_BYTES,
        )
        return None
    try:
        async with session.get(
            attachment.url, timeout=aiohttp.ClientTimeout(total=60)
        ) as response:
            if response.status != 200:
                log.warning("Could not download %s (%s)", attachment.url, response.status)
                return None
            data = await read_body_limited(response, IMAGE_SOURCE_LIMIT_BYTES)
            if data is None:
                log.warning("Skipping %s: larger than %d bytes", attachment.filename, IMAGE_SOURCE_LIMIT_BYTES)
                return None
            if not data:
                log.warning("Downloaded an empty body for %s", attachment.filename)
                return None
            content_type = attachment.content_type or "image/jpeg"
            return await asyncio.to_thread(
                compress_image, data, content_type, attachment.filename
            )
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.exception("Download of Discord attachment %s failed", attachment.url)
        return None


def compress_image(
    data: bytes, content_type: str, label: str
) -> tuple[bytes, str] | None:
    """Return bytes ready for GroupMe, shrinking oversized images.

    Images already within `IMAGE_UPLOAD_LIMIT_BYTES` and `IMAGE_MAX_DIMENSION` are
    passed through untouched. Larger ones are downscaled and re-encoded as JPEG,
    lowering quality until they fit. Returns None when the image cannot be handled.
    """
    if Image is None:
        if len(data) <= IMAGE_UPLOAD_LIMIT_BYTES:
            return data, content_type
        log.warning("Pillow is missing; skipping oversized image %s", label)
        return None

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        log.warning("Could not decode %s (%s)", label, content_type)
        return (data, content_type) if len(data) <= IMAGE_UPLOAD_LIMIT_BYTES else None

    try:
        width, height = image.size
        too_large = max(width, height) > IMAGE_MAX_DIMENSION
        too_heavy = len(data) > IMAGE_UPLOAD_LIMIT_BYTES
        if not too_large and not too_heavy:
            return data, content_type

        animated = bool(getattr(image, "is_animated", False))
        if animated:
            log.warning(
                "Keeping animated %s untouched (%.1f MB, %dx%d); re-encoding would drop frames",
                label,
                len(data) / 1048576,
                width,
                height,
            )
            return (data, content_type) if not too_heavy else None

        if image.mode in {"RGBA", "LA", "P"}:
            rgba = image.convert("RGBA")
            converted = Image.new("RGB", rgba.size, (255, 255, 255))
            converted.paste(rgba, mask=rgba.split()[-1])
        else:
            converted = image.convert("RGB")

        if too_large:
            resample = getattr(Image, "Resampling", Image).LANCZOS
            converted.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), resample)

        quality = IMAGE_JPEG_QUALITY
        while True:
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=quality, optimize=True)
            payload = buffer.getvalue()
            if len(payload) <= IMAGE_UPLOAD_LIMIT_BYTES or quality <= 40:
                break
            quality -= 10

        log.info(
            "Compressed %s: %.1f MB %dx%d -> %.1f MB %dx%d (q%d)",
            label,
            len(data) / 1048576,
            width,
            height,
            len(payload) / 1048576,
            converted.width,
            converted.height,
            quality,
        )
        return payload, "image/jpeg"
    finally:
        image.close()


# --------------------------------------------------------------------------- #
# Relay logic
# --------------------------------------------------------------------------- #


def bridged_channel_id(message: discord.Message) -> int:
    """The Discord destination a message was sent in: a channel, or a forum post/thread."""
    return message.channel.id


def oldest_numeric_id(messages: list[dict]) -> str:
    """Smallest numeric message id in a page, or "" when ids are not numeric."""
    ids = [str(message.get("id") or "") for message in messages]
    if not ids or not all(message_id.isdigit() for message_id in ids):
        return ""
    return str(min(int(message_id) for message_id in ids))


def message_id_at_least(message_id: str, boundary: str) -> bool:
    """Whether `message_id` falls inside a page whose oldest entry is `boundary`."""
    try:
        return int(message_id) >= int(boundary)
    except (TypeError, ValueError):
        return False


async def record_bot_posts(
    session: aiohttp.ClientSession,
    conversation_id: str,
    discord_id: int,
    posts: list[tuple[str, str]],
) -> None:
    """Find the GroupMe ids of bot posts so a Discord deletion can remove them.

    `POST /bots/post` returns no message, so the newest bot messages are matched
    by exact text and, for picture posts, by image URL. This is best-effort: a
    message that cannot be found simply has no GroupMe id to delete later.
    """
    if not posts or not GROUPME_ACCESS_TOKEN:
        return
    try:
        messages = await groupme_list_messages(session, conversation_id, limit=50)
    except GroupMeError:
        log.debug(
            "Could not look up GroupMe ids for Discord message %s", discord_id, exc_info=True
        )
        return

    cutoff = time.time() - 300
    claimed: set[str] = set()
    for text, picture_url in posts:
        for message in messages:
            message_id = str(message.get("id") or "")
            if not message_id or message_id in claimed:
                continue
            # Never attach an older bot post to a newer Discord message.
            if outbound_groupme_id_exists(conversation_id, message_id):
                continue
            if not is_groupme_bot_message(message):
                continue
            if (message.get("text") or "") != text:
                continue
            image_urls = groupme_image_urls(message)
            if picture_url:
                if picture_url not in image_urls:
                    continue
            elif image_urls:
                continue
            try:
                if int(message.get("created_at") or 0) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            claimed.add(message_id)
            add_outbound(conversation_id, discord_id, message_id, utcnow())
            break


async def relay_discord_message(
    session: aiohttp.ClientSession,
    message: discord.Message,
    bridge: sqlite3.Row,
    client=None,
) -> None:
    name = message.author.display_name or message.author.name
    text = render_discord_content(message)
    images = [a for a in message.attachments if is_image_attachment(a)]
    links = [a.url for a in message.attachments if not is_image_attachment(a)]

    # The nickname always leads the message so GroupMe members see who typed it,
    # including on picture-only posts (the bot posts those separately).
    header = f"{name}:"
    quote = quoted_reply(message)
    if quote:
        body = f"{header}\n{quote}{text}".rstrip()
    else:
        body = f"{header} {text}".rstrip()
    if links:
        body = f"{body}\n" + "\n".join(links)
    if len(images) > MAX_GROUPME_IMAGES:
        overflow = [a.url for a in images[MAX_GROUPME_IMAGES:]]
        body = f"{body}\n" + "\n".join(overflow)
    images = images[:MAX_GROUPME_IMAGES]

    # GroupMe only accepts i.groupme.com image URLs ("invalid origin" otherwise), so
    # every image is compressed and re-hosted before the relay; failures are reported.
    hosted: list[str] = []
    failed = 0
    for attachment in images:
        url = await host_image_for_groupme(session, attachment)
        if url is None:
            failed += 1
        else:
            hosted.append(url)

    note = f"\n[{failed} image(s) could not be relayed]" if failed else ""
    if note:
        body = truncate(body, GROUPME_TEXT_LIMIT - len(note)) + note
    else:
        body = truncate(body)

    if bridge["send_mode"] == "user":
        await send_as_user(session, bridge, body, hosted, discord_id=message.id)
    else:
        bot_id = bridge["bot_id"]
        try:
            await groupme_post_bot(session, bot_id, body)
        except GroupMeError as error:
            # GroupMe answers 404 when the bot id no longer exists (deleted, recreated,
            # or copied from another account). Repair the, client bridge and post once more.
            if error.status != 404:
                raise
            bot_id = await recover_bot_id(session, bridge)
            await groupme_post_bot(session, bot_id, body)
        posts: list[tuple[str, str]] = [(body, "")]
        for image_url in hosted:
            await groupme_post_bot(session, bot_id, header, picture_url=image_url)
            posts.append((header, image_url))
        await record_bot_posts(
            session, outbound_conversation_id(bridge), message.id, posts
        )

    log.info(
        "Relayed Discord message %s from #%s to GroupMe conversation %s (%d image(s), %d failed)",
        message.id,
        bridge["channel_id"],
        outbound_conversation_id(bridge),
        len(hosted),
        failed,
    )


async def recover_bot_id(
    session: aiohttp.ClientSession, bridge: sqlite3.Row, client=None
) -> str:
    """Find or create a working bot for the bridge's group and store it."""
    group_id = bridge["group_id"]
    existing = await groupme_find_bot(session, group_id, callback_url())
    bot_id = str((existing or {}).get("bot_id") or "")
    if not bot_id:
        created = await groupme_create_bot_with_fallback(
            session, group_id, await fresh_bot_avatar(session, client)
        )
        bot_id = str(created.get("bot_id") or "")
    if not bot_id:
        raise GroupMeError(f"could not obtain a GroupMe bot for group {group_id}")
    set_bridge_bot(bridge["conversation_id"], bot_id)
    log.warning(
        "Replaced stale GroupMe bot id for group %s with %s", group_id, bot_id
    )
    return bot_id


async def host_image_for_groupme(
    session: aiohttp.ClientSession, attachment: discord.Attachment
) -> str | None:
    """Download, compress, and re-host a Discord image on GroupMe's image service."""
    if not GROUPME_ACCESS_TOKEN:
        log.warning(
            "GROUPME_ACCESS_TOKEN is unset; %s cannot be relayed (GroupMe only accepts i.groupme.com images)",
            attachment.filename,
        )
        return None
    downloaded = await download_discord_image(session, attachment)
    if downloaded is None:
        return None
    data, content_type = downloaded
    try:
        return await groupme_upload_image(session, data, content_type)
    except GroupMeError:
        log.exception("GroupMe image upload failed for %s", attachment.filename)
        return None


def read_avatar_file(path: str) -> tuple[bytes, str] | None:
    """Read the configured local avatar image, resolved relative to bot.py."""
    full_path = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    try:
        with open(full_path, "rb") as handle:
            data = handle.read(IMAGE_SOURCE_LIMIT_BYTES + 1)
    except OSError:
        log.warning("GROUPME_BOT_AVATAR_PATH %s could not be read", full_path)
        return None
    if not data:
        log.warning("GROUPME_BOT_AVATAR_PATH %s is empty", full_path)
        return None
    if len(data) > IMAGE_SOURCE_LIMIT_BYTES:
        log.warning("GROUPME_BOT_AVATAR_PATH %s is larger than %d bytes", full_path, IMAGE_SOURCE_LIMIT_BYTES)
        return None
    suffix = os.path.splitext(full_path)[1].lower()
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")
    return data, content_type


async def fresh_bot_avatar(session: aiohttp.ClientSession, client=None) -> str:
    """Upload a brand new copy of the bot avatar and return its i.groupme.com URL.

    GroupMe deletes a hosted image when a bot using it is destroyed, so avatars are
    never shared: each bot creation uploads its own copy. Sources in priority order:
    `GROUPME_BOT_AVATAR_PATH` (a file in the repo), `GROUPME_BOT_AVATAR_URL`, and
    finally the Discord bot's own picture.
    """
    if not GROUPME_ACCESS_TOKEN:
        return ""

    if GROUPME_BOT_AVATAR_PATH:
        local = read_avatar_file(GROUPME_BOT_AVATAR_PATH)
        if local is not None:
            data, content_type = local
            try:
                return await groupme_upload_image(session, data, content_type)
            except GroupMeError:
                log.exception("Could not upload the local bot avatar")

    sources: list[str] = []
    if GROUPME_BOT_AVATAR_URL:
        sources.append(GROUPME_BOT_AVATAR_URL)
    if client is not None and getattr(client, "user", None) is not None:
        sources.append(client.user.display_avatar.with_size(256).url)

    for url in sources:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    log.warning("Avatar source %s returned %s", url, response.status)
                    continue
                data = await read_body_limited(response, IMAGE_SOURCE_LIMIT_BYTES)
                content_type = response.content_type or "image/png"
            if not data:
                continue
            return await groupme_upload_image(session, data, content_type)
        except (aiohttp.ClientError, GroupMeError, asyncio.TimeoutError):
            log.exception("Could not re-host avatar %s", url)
    return ""


async def send_as_user(
    session: aiohttp.ClientSession,
    bridge: sqlite3.Row,
    body: str,
    image_urls: list[str],
    *,
    discord_id: int | None = None,
) -> None:
    """Post with the access token instead of a bot.

    Used for topics (which cannot host bots) and for groups that refuse bot creation.
    """
    if not GROUPME_ACCESS_TOKEN:
        raise GroupMeError(
            "GROUPME_ACCESS_TOKEN is required to post as a GroupMe user"
        )
    attachments = [{"type": "image", "url": url} for url in image_urls]

    conversation_id = outbound_conversation_id(bridge)
    created = await groupme_post_message(session, conversation_id, body, attachments)
    if created and created.get("id"):
        # Remember the id so the poller does not mirror our own post back to Discord.
        message_id = str(created["id"])
        RECENT_OUTBOUND[message_id] = time.monotonic()
        add_link(
            LINK_GROUPME,
            conversation_id,
            int(bridge["channel_id"]),
            message_id,
            None,
            utcnow(),
        )
        if discord_id is not None:
            add_outbound(conversation_id, discord_id, message_id, utcnow())
        cutoff = time.monotonic() - 900
        for key in [key for key, stamp in RECENT_OUTBOUND.items() if stamp < cutoff]:
            RECENT_OUTBOUND.pop(key, None)


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #

bridge_group = app_commands.Group(
    name="bridge",
    description="Manage GroupMe <-> Discord bridge links.",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


def parse_group_id(value: str) -> str:
    """Accept a GroupMe group id or a join/share URL."""
    value = value.strip().rstrip("/")
    match = re.search(r"(\d{5,})", value)
    return match.group(1) if match else value


def describe_commands(tree: app_commands.CommandTree) -> str:
    """List the slash commands this process publishes, for the startup log."""
    names: list[str] = []
    for command in tree.get_commands():
        if isinstance(command, app_commands.Group):
            names.extend(f"{command.name} {child.name}" for child in command.commands)
        else:
            names.append(command.name)
    return ", ".join(sorted(names)) or "none"


def callback_url() -> str:
    """Public callback URL, usable even before the tunnel starts (e.g. from scripts)."""
    base = PUBLIC_BASE_URL or (f"https://{NGROK_DOMAIN}" if NGROK_DOMAIN else "")
    if not base:
        return ""
    return f"{base.rstrip('/')}{GROUPME_WEBHOOK_PATH}"


def find_topic(topics: list[dict], query: str) -> dict | None:
    """Match a topic by id, exact name, then substring."""
    query = query.strip().lstrip("#").casefold()
    if not query:
        return None
    for topic in topics:
        if str(topic.get("id")) == query:
            return topic
    for topic in topics:
        if (topic.get("topic") or "").strip().casefold() == query:
            return topic
    for topic in topics:
        if query in (topic.get("topic") or "").strip().casefold():
            return topic
    return None


async def topic_choices(
    interaction: discord.Interaction, group_id: str, current: str
) -> list[app_commands.Choice[str]]:
    if not GROUPME_ACCESS_TOKEN or not str(group_id).isdigit():
        return []
    try:
        topics = await groupme_list_subgroups(
            interaction.client.groupme_session, str(group_id)
        )
    except (GroupMeError, aiohttp.ClientError):
        return []
    current = (current or "").casefold().lstrip("#")
    choices = []
    for topic in topics:
        name = str(topic.get("topic") or topic.get("id"))
        topic_id = str(topic.get("id"))
        if current in name.casefold() or current in topic_id:
            choices.append(app_commands.Choice(name=f"#{name}"[:100], value=name[:100]))
    return choices[:25]


async def topic_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Offer the topics of whichever group id is already typed in the command."""
    group_id = parse_group_id(str(getattr(interaction.namespace, "group_id", "") or ""))
    return await topic_choices(interaction, group_id, current)


async def topic_autocomplete_here(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Offer the topics of the group behind the bridge on this channel or post."""
    bridge = bridge_for_destination(getattr(interaction.channel, "id", 0) or 0)
    if bridge is None:
        return []
    return await topic_choices(interaction, bridge["group_id"], current)


class TargetError(Exception):
    """The command was run somewhere that cannot host a bridge."""


def resolve_target(channel) -> tuple[str, int, int | None]:
    """Describe where the command was run: `(kind, destination_id, parent_id)`.

    `kind` is `'channel'` for a text channel, `'thread'` for a forum post or thread,
    and `'forum'` when the command ran on the forum itself, in which case the caller
    creates a post and uses that instead.
    """
    if isinstance(channel, discord.Thread):
        return "thread", channel.id, channel.parent_id
    if isinstance(channel, discord.TextChannel):
        return "channel", channel.id, None
    if isinstance(channel, discord.ForumChannel):
        return "forum", channel.id, None
    raise TargetError(
        "Run this command in a text channel, a forum channel, or a forum post."
    )


async def target_or_reply(interaction: discord.Interaction):
    try:
        return resolve_target(interaction.channel)
    except TargetError as error:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Unsupported channel",
                description=str(error),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return None


async def bridge_for_target_or_reply(interaction: discord.Interaction):
    """The bridge attached to the channel, forum post, or thread the command ran in."""
    target = await target_or_reply(interaction)
    if target is None:
        return None
    bridge = bridge_for_destination(target[1])
    if bridge is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Nothing bridged here",
                description=(
                    "This channel or post has no GroupMe bridge yet. Run `/bridge add` "
                    "here to create one."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return None
    return bridge


async def require_manager(interaction: discord.Interaction) -> bool:
    """Bridge management is restricted to server administrators."""
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.guild_permissions.administrator:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Missing permission",
                description="Managing GroupMe bridges needs the Administrator permission.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return False
    return True


@bridge_group.command(
    name="add",
    description="Bridge a GroupMe group or one of its topics to this channel or forum post.",
)
@app_commands.autocomplete(topic=topic_autocomplete)
@app_commands.describe(
    group_id="GroupMe group id (or a groupme.com join link)",
    topic="Optional GroupMe topic name or id to mirror instead of the main chat",
    bot_id="Optional GroupMe bot id; leave empty to create one automatically",
)
async def bridge_add(
    interaction: discord.Interaction,
    group_id: str,
    topic: str = "",
    bot_id: str = "",
):
    """Bridge into wherever the command runs: a channel, a forum channel, or a post."""
    if not await require_manager(interaction):
        return
    target = await target_or_reply(interaction)
    if target is None:
        return
    kind, destination_id, parent_id = target

    group_id = parse_group_id(group_id)
    bot_id = (bot_id or "").strip()
    topic_query = (topic or "").strip()

    if kind != "forum":
        already = bridge_for_destination(destination_id)
        if already is not None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Destination already bridged",
                    description=(
                        f"This {'post' if kind == 'thread' else 'channel'} already mirrors "
                        f"**{conversation_label(already)}**. Bridges are one-to-one: remove "
                        "that bridge first, or use `/bridge set-topic` to point it elsewhere."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

    if not topic_query and not bot_id and not GROUPME_ACCESS_TOKEN:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Cannot create a GroupMe bot",
                description=(
                    "`bot_id` is optional only when `GROUPME_ACCESS_TOKEN` is set, "
                    "because the bot creates one for you. Pass a `bot_id` instead."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    session = interaction.client.groupme_session
    group: dict | None = None
    warnings: list[str] = []
    if GROUPME_ACCESS_TOKEN:
        try:
            group = await groupme_get_group(session, group_id)
        except GroupMeError as error:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Group lookup failed",
                    description=f"GroupMe rejected the group lookup: `{error}`",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        if group is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Unknown group",
                    description=f"GroupMe group `{group_id}` was not found for this access token.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
    else:
        warnings.append("`GROUPME_ACCESS_TOKEN` is unset, so the group could not be verified.")

    topic_row: dict | None = None
    if topic_query:
        if not GROUPME_ACCESS_TOKEN:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Access token required",
                    description="Topic bridges need `GROUPME_ACCESS_TOKEN`; topics cannot host bots.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        try:
            topics = await groupme_list_subgroups(session, group_id)
        except GroupMeError as error:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Topic lookup failed",
                    description=f"GroupMe rejected the subgroup lookup: `{error}`",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        topic_row = find_topic(topics, topic_query)
        if topic_row is None:
            available = (
                "\n".join(
                    f"• **{t.get('topic') or 'unnamed'}** — `{t.get('id')}`" for t in topics
                )
                or "This group has no topics."
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Topic not found",
                    description=f"No topic matches `{topic_query}`.\n\n{available}",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        warnings.append(
            "Topic bridges post with `GROUPME_ACCESS_TOKEN`, so Discord messages appear "
            "from the authenticated GroupMe account, not a bot."
        )

    conversation_id = str(topic_row.get("id")) if topic_row else group_id
    conversation_type = "topic" if topic_row else "group"
    group_name = (group or {}).get("name") or ""
    conversation_name = (
        (topic_row.get("topic") or "") if topic_row else (group_name or group_id)
    )

    clash = bridge_for_conversation(conversation_id)
    if clash is not None:
        await interaction.followup.send(
            embed=discord.Embed(
                title="⚠️ Conversation already bridged",
                description=(
                    f"**{conversation_label(clash)}** already mirrors "
                    f"{destination_label(clash)}. Every GroupMe conversation maps to one "
                    "Discord destination, so remove that bridge first."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    if topic_row:
        # Topics cannot host bots: Discord messages are posted with the access token and
        # the topic is polled, so a GroupMe bot plays no part in a topic bridge.
        send_mode = "user"
        if bot_id:
            bot_id = ""
            warnings.append(
                "A topic bridge posts with `GROUPME_ACCESS_TOKEN`; `bot_id` is ignored."
            )
    else:
        send_mode = "bot"
        if bot_id:
            # Validate an explicit id so a typo does not fail later with a 404 on post.
            if GROUPME_ACCESS_TOKEN:
                try:
                    bots = await groupme_list_bots(session)
                except GroupMeError:
                    bots = []
                if bots and not any(str(b.get("bot_id")) == bot_id for b in bots):
                    warnings.append(
                        f"Bot `{bot_id}` was not found on this GroupMe account; "
                        "posting may fail with 404."
                    )
        else:
            # Main-chat bridges post through a GroupMe bot when one is available.
            resolved, note = await ensure_group_bot(
                session, group_id, await fresh_bot_avatar(session, interaction.client)
            )
            if note:
                warnings.append(note)
            if resolved:
                bot_id = resolved
            elif GROUPME_ACCESS_TOKEN:
                # Some groups refuse bots entirely (GroupMe answers 401 for that group even
                # in its own UI). The access token can still post into the main chat, so the
                # bridge keeps working both ways: the inbound side is polled instead.
                send_mode = "user"
                warnings.append(
                    "GroupMe would not create a bot for this group, so Discord messages are "
                    "posted with `GROUPME_ACCESS_TOKEN` and appear as your GroupMe account. "
                    "GroupMe messages are polled every "
                    f"{GROUPME_POLL_SECONDS}s instead of arriving by callback."
                )
            else:
                hint = ""
                lowered = note.lower()
                if "401" in lowered or "unauthor" in lowered:
                    owned = await groupme_owns_group(session, group_id)
                    if owned is False:
                        hint = (
                            f"\n\n`{group_id}` is not one of the groups this access token is "
                            "in, so GroupMe will not let it host a bot. That usually means the "
                            "id is a **topic** (subgroup) rather than a group: pass the parent "
                            "group id and select the topic, "
                            "`/bridge add group_id:<parent group> topic:<name>`. "
                            "`/bridge topics group_id:<parent group>` lists them."
                        )
                    else:
                        hint = (
                            "\n\nGroupMe answers **401** for `POST /bots` when "
                            "`GROUPME_ACCESS_TOKEN` is not valid for bot calls, and for topic "
                            "(subgroup) ids, which cannot host bots. Copy a fresh token from "
                            "https://dev.groupme.com/applications into `.env`, restart the bot, "
                            "and use `topic:<name>` if you meant a topic."
                        )
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Bot creation failed",
                        description=(
                            f"{note}{hint}\n\n"
                            "Pass an existing `bot_id`, or create one with "
                            f"`python utils/groupme_setup.py create-bot --group-id {group_id} "
                            f"--callback-url {callback_url() or 'https://<your-host>' + GROUPME_WEBHOOK_PATH}` "
                            "and run the command again."
                        ),
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return
    if bot_id and not callback_url() and GROUPME_MODE == "webhook":
        warnings.append(
            "The bot has no callback URL; set `GROUPME_PUBLIC_URL` or `NGROK_DOMAIN` so "
            "group messages arrive."
        )

    # A forum channel is not a destination itself: create the post that the bridge
    # will live in, then treat it like any other thread target.
    target_kind = "thread" if kind in {"thread", "forum"} else "channel"
    if topic_row:
        mirrored_label = f"#{conversation_name} in group {group_name or group_id}"
    else:
        mirrored_label = group_name or group_id
    if kind == "forum":
        forum = interaction.channel
        post_name = (
            f"{group_name or group_id} · #{conversation_name}"
            if topic_row
            else (group_name or group_id)
        )
        try:
            created = await forum.create_thread(
                name=truncate(post_name, 100),
                content=f"GroupMe bridge: {mirrored_label}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Cannot create a post",
                    description=(
                        "The bot needs Create Posts (and View Channel) in this forum channel "
                        "to make the post the bridge will live in."
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Could not create the post",
                    description=f"Discord rejected the request: `{error}`",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        destination_id = created.thread.id
        parent_id = forum.id

    webhook_channel_id = int(parent_id) if target_kind == "thread" and parent_id else destination_id
    try:
        webhook_channel = interaction.client.get_channel(
            webhook_channel_id
        ) or await interaction.client.fetch_channel(webhook_channel_id)
        webhook = await webhook_channel.create_webhook(
            name=WEBHOOK_NAME, reason="Catbird GroupMe bridge"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Webhook permission missing",
                description=(
                    "The bot needs Manage Webhooks in this channel to mirror GroupMe users. "
                    "Grant it and try again."
                ),
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return
    except (discord.HTTPException, AttributeError) as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Could not create webhook",
                description=f"Discord rejected the webhook request: `{error}`",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    save_bridge(
        conversation_id,
        destination_id,
        bot_id,
        conversation_name,
        conversation_type=conversation_type,
        group_id=group_id,
        group_name=group_name,
        target_kind=target_kind,
        parent_id=parent_id if target_kind == "thread" else None,
        webhook_id=webhook.id,
        webhook_token=webhook.token,
        send_mode=send_mode,
    )
    interaction.client.webhooks[destination_id] = webhook

    # Baseline only the conversation this bridge mirrors: everything older is history.
    source = topic_row if topic_row else group
    last_message_id = ((source or {}).get("messages") or {}).get("last_message_id")
    if last_message_id:
        set_last_seen(conversation_id, str(last_message_id))
    else:
        warnings.append(
            "No message baseline was recorded; existing GroupMe history may be replayed."
        )

    await interaction.client.refresh_bot_names()

    destination_text = f"<#{destination_id}>"
    if target_kind == "thread":
        destination_text += " (forum post)" if kind == "forum" else " (thread)"
    description = f"{destination_text} now mirrors **{mirrored_label}**."
    if topic_row:
        description += (
            "\nOnly this topic is mirrored. Discord messages here are posted into it."
        )
    else:
        description += "\nOnly the group's main chat is mirrored; topics stay separate."
    if warnings:
        description += "\n\n" + "\n".join(f"⚠️ {warning}" for warning in warnings)
    if bot_id and GROUPME_MODE == "webhook":
        url = callback_url()
        if url:
            description += (
                f"\n\nSet the bot's callback URL to `{url}` "
                f"(bot id `{bot_id}`), otherwise GroupMe messages will not arrive."
            )
        else:
            description += (
                "\n\nSet `GROUPME_PUBLIC_URL` so this command can print the bot callback URL."
            )
    await interaction.followup.send(
        embed=discord.Embed(title="✅ Bridge created", description=description, color=discord.Color.green()),
        ephemeral=True,
    )


@bridge_group.command(name="topics", description="List the topics (subgroups) of a GroupMe group.")
@app_commands.describe(group_id="GroupMe group id (or a groupme.com join link)")
async def bridge_topics(interaction: discord.Interaction, group_id: str):
    if not await require_manager(interaction):
        return
    if not GROUPME_ACCESS_TOKEN:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Access token required",
                description="`GROUPME_ACCESS_TOKEN` is needed to read a group's topics.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return
    group_id = parse_group_id(group_id)
    await interaction.response.defer(ephemeral=True)
    try:
        topics = await groupme_list_subgroups(interaction.client.groupme_session, group_id)
    except GroupMeError as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Topic lookup failed",
                description=f"`{error}`",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"Topics in group {group_id}", color=discord.Color.blurple()
    )
    if not topics:
        embed.description = "This group has no topics."
    for topic in topics[:25]:
        count = ((topic.get("messages") or {}).get("count")) or 0
        embed.add_field(
            name=str(topic.get("topic") or "unnamed"),
            value=f"id `{topic.get('id')}`\n{count} message(s)",
            inline=True,
        )
    embed.set_footer(text="Use /bridge add with topic:<name> to bridge one topic")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bridge_group.command(
    name="set-topic",
    description="Mirror a different GroupMe topic here, or switch back to the main chat.",
)
@app_commands.autocomplete(topic=topic_autocomplete_here)
@app_commands.describe(
    topic="Topic name or id (use 'none' to mirror the group's main chat instead)",
)
async def bridge_set_topic(interaction: discord.Interaction, topic: str):
    """Retarget the bridge on this channel or post at another GroupMe conversation."""
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return

    await interaction.response.defer(ephemeral=True)
    session = interaction.client.groupme_session
    group_id = str(bridge["group_id"])
    group_name = bridge["group_name"]
    destination = int(bridge["channel_id"])
    topic_query = (topic or "").strip()
    if topic_query.casefold() in {"", "none", "clear", "off", "main"}:
        if bridge["conversation_type"] == "group":
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Already the main chat",
                    description=(
                        f"<#{destination}> already mirrors the main chat of "
                        f"**{group_name or group_id}**."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        clash = bridge_for_conversation(group_id)
        if clash is not None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Main chat already bridged",
                    description=(
                        f"The main chat of **{group_name or group_id}** already mirrors "
                        f"{destination_label(clash)}. Remove that bridge first."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        bot_id = bridge["bot_id"]
        if not bot_id:
            # The main chat is normally posted through a GroupMe bot, so make sure one
            # exists; a group that refuses bots falls back to posting with the token.
            bot_id, note = await ensure_group_bot(
                session, group_id, await fresh_bot_avatar(session, interaction.client)
            )
            if not bot_id and not GROUPME_ACCESS_TOKEN:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ No GroupMe bot",
                        description=(
                            f"{note}\n\nThe main chat is posted through a GroupMe bot. Create "
                            "one with `utils/groupme_setup.py`, then run `/bridge set-bot`."
                        ),
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return
        send_mode = "bot" if bot_id else "user"
        update_bridge_conversation(
            bridge["conversation_id"],
            group_id,
            "group",
            group_name or group_id,
            send_mode,
            bot_id=bot_id,
        )
        group = await groupme_get_group(session, group_id)
        last = ((group or {}).get("messages") or {}).get("last_message_id")
        if last:
            set_last_seen(group_id, str(last))
        via = "" if bot_id else " Messages are posted with `GROUPME_ACCESS_TOKEN`."
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Now mirroring the main chat",
                description=(
                    f"<#{destination}> now mirrors the main chat of "
                    f"**{group_name or group_id}**. Topic messages are no longer relayed "
                    f"here.{via}"
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
        return

    if not GROUPME_ACCESS_TOKEN:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Access token required",
                description="Topic bridges need `GROUPME_ACCESS_TOKEN`; topics cannot host bots.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    try:
        topics = await groupme_list_subgroups(interaction.client.groupme_session, bridge["group_id"])
    except GroupMeError as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Topic lookup failed", description=f"`{error}`", color=discord.Color.red()
            ),
            ephemeral=True,
        )
        return

    topic_row = find_topic(topics, topic_query)
    if topic_row is None:
        available = (
            "\n".join(f"• **{t.get('topic') or 'unnamed'}** — `{t.get('id')}`" for t in topics)
            or "This group has no topics."
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Topic not found",
                description=f"No topic matches `{topic_query}`.\n\n{available}",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    topic_id = str(topic_row.get("id"))
    topic_name = topic_row.get("topic") or topic_id
    if topic_id == str(bridge["conversation_id"]):
        await interaction.followup.send(
            embed=discord.Embed(
                title="⚠️ Already bridged",
                description=f"<#{destination}> already mirrors topic **{topic_name}**.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return
    clash = bridge_for_conversation(topic_id)
    if clash is not None:
        await interaction.followup.send(
            embed=discord.Embed(
                title="⚠️ Topic already bridged",
                description=(
                    f"Topic **{topic_name}** already mirrors {destination_label(clash)}. "
                    "Remove that bridge first."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return
    update_bridge_conversation(
        bridge["conversation_id"],
        topic_id,
        "topic",
        topic_name,
        "user",
    )
    topic_last = (topic_row.get("messages") or {}).get("last_message_id")
    if topic_last:
        set_last_seen(topic_id, str(topic_last))

    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Topic updated",
            description=(
                f"<#{destination}> now mirrors topic **{topic_name}** (`{topic_id}`) of "
                f"**{group_name or group_id}**.\n"
                "Discord messages here are posted into that topic, and GroupMe messages "
                "from it are no longer relayed alongside the main chat."
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="remove", description="Remove the bridge on this channel or post.")
async def bridge_remove(interaction: discord.Interaction):
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return

    await interaction.response.defer(ephemeral=True)
    interaction.client.webhooks.pop(int(bridge["channel_id"]), None)
    if bridge["webhook_id"]:
        try:
            webhook = await interaction.client.fetch_webhook(int(bridge["webhook_id"]))
            await webhook.delete(reason="GroupMe bridge removed")
        except (discord.NotFound, discord.Forbidden):
            log.warning(
                "Could not delete webhook %s while removing %s",
                bridge["webhook_id"],
                bridge["conversation_id"],
            )
    delete_bridge(bridge["conversation_id"])
    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Bridge removed",
            description=(
                f"Stopped mirroring **{conversation_label(bridge)}** to "
                f"<#{bridge['channel_id']}>."
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(
    name="set-mode",
    description="Choose how Discord messages are posted into GroupMe for this bridge.",
)
@app_commands.describe(
    mode=(
        "user posts as the access-token account (GroupMe returns the message id, so "
        "Discord deletions remove it); bot posts as the GroupMe bot"
    )
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="user (deletable)", value="user"),
        app_commands.Choice(name="bot", value="bot"),
    ]
)
async def bridge_set_mode(
    interaction: discord.Interaction, mode: app_commands.Choice[str]
):
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return
    await interaction.response.defer(ephemeral=True)
    if bridge["conversation_type"] == "topic":
        await interaction.followup.send(
            embed=discord.Embed(
                title="⚠️ Topic bridge",
                description=(
                    "Topic bridges always post with `GROUPME_ACCESS_TOKEN`, so there is no "
                    "bot mode to switch to."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    if mode.value == "user":
        if not GROUPME_ACCESS_TOKEN:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ No access token",
                    description=(
                        "`GROUPME_ACCESS_TOKEN` is unset, so GroupMe messages cannot be "
                        "posted as the account."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        set_bridge_send_mode(bridge["conversation_id"], "user")
        description = (
            f"**{conversation_label(bridge)}** now posts with `GROUPME_ACCESS_TOKEN`.\n\n"
            "GroupMe returns the message id, so deleting the Discord message deletes the "
            "GroupMe copy directly. Discord messages now appear from your GroupMe account "
            "instead of the bot."
        )
        if bridge["bot_id"]:
            description += (
                "\n\nThe bot stays attached for inbound callbacks, so GroupMe messages "
                "still arrive instantly."
            )
    else:
        bot_id = str(bridge["bot_id"] or "")
        note = ""
        if not bot_id and GROUPME_MODE == "webhook" and not callback_url():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ No callback URL",
                    description=(
                        "This bridge has no bot and no public callback URL is configured, so "
                        "GroupMe messages currently arrive by polling. Creating a bot now "
                        "would stop the polling before callbacks work. Set `NGROK_DOMAIN` or "
                        "`GROUPME_PUBLIC_URL` first, or stay in user mode."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        if not bot_id:
            bot_id, note = await ensure_group_bot(
                interaction.client.groupme_session,
                bridge["group_id"],
                await fresh_bot_avatar(
                    interaction.client.groupme_session, interaction.client
                ),
            )
            if not bot_id:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ No GroupMe bot",
                        description=f"GroupMe would not provide a bot. {note}",
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return
            set_bridge_bot(bridge["conversation_id"], bot_id)
            await interaction.client.refresh_bot_names()
        set_bridge_send_mode(bridge["conversation_id"], "bot")
        description = f"**{conversation_label(bridge)}** now posts with bot `{bot_id}`."
        if GROUPME_MODE == "webhook" and bot_id:
            description += "\n\nGroupMe messages keep arriving through the bot's callback."
        if note:
            description += f"\n\n{note}"

    log.info(
        "Bridge %s switched to send_mode=%s via /bridge set-mode",
        bridge["conversation_id"],
        mode.value,
    )
    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Posting mode updated",
            description=description,
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="set-bot", description="Change the GroupMe bot used by this bridge.")
@app_commands.describe(bot_id="New GroupMe bot id")
async def bridge_set_bot(interaction: discord.Interaction, bot_id: str):
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return
    if bridge["conversation_type"] == "topic":
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Topic bridge",
                description=(
                    "This bridge posts into a GroupMe topic with `GROUPME_ACCESS_TOKEN`, so it "
                    "has no bot to change. Run `/bridge set-topic none` to switch it to the "
                    "main chat first."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return
    set_bridge_bot(bridge["conversation_id"], bot_id.strip())
    await interaction.client.refresh_bot_names()
    await interaction.response.send_message(
        embed=discord.Embed(
            title="✅ Bot updated",
            description=(
                f"**{conversation_label(bridge)}** now posts with bot `{bot_id.strip()}`."
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="list", description="List every GroupMe <-> Discord bridge.")
async def bridge_list(interaction: discord.Interaction):
    if not await require_manager(interaction):
        return
    bridges = list_bridges()
    embed = discord.Embed(title="GroupMe bridges", color=discord.Color.blurple())
    if not bridges:
        embed.description = "No bridges yet. Run `/bridge add` inside a channel or a forum post."
    for bridge in bridges[:25]:
        source = conversation_label(bridge)
        if bridge["conversation_type"] == "topic":
            source += f"\nof group `{bridge['group_id']}`"
        poster = (
            "access token"
            if bridge["send_mode"] == "user"
            else f"bot `{bridge['bot_id']}`"
        )
        if bridge["send_mode"] == "user" and bridge["bot_id"]:
            poster += f" (callbacks via bot `{bridge['bot_id']}`)"
        embed.add_field(
            name=truncate(bridge["group_name"] or source, 100),
            value=f"{destination_label(bridge)}\n{source}\nposts via {poster}",
            inline=True,
        )
    embed.set_footer(text=f"{len(bridges)} bridge(s) · {GROUPME_MODE} mode")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bridge_group.command(
    name="set-avatar",
    description="Give the bridge's GroupMe bot the Discord bot's picture (recreates the bot).",
)
@app_commands.describe(
    image_url="Optional image URL; defaults to the Discord bot's avatar",
)
async def bridge_set_avatar(interaction: discord.Interaction, image_url: str = ""):
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return
    if not bridge["bot_id"]:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ No GroupMe bot",
                description=(
                    "This bridge posts into a GroupMe topic with the access token, so it has "
                    "no bot avatar to set."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    client = interaction.client
    session = client.groupme_session
    try:
        avatar = (
            await client.host_avatar(image_url.strip())
            if image_url.strip()
            else await fresh_bot_avatar(session, client)
        )
    except (GroupMeError, aiohttp.ClientError) as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Avatar upload failed",
                description=f"`{error}`",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return
    if not avatar:
        await interaction.followup.send(
            embed=discord.Embed(
                title="⚠️ No avatar available",
                description="Pass an `image_url`, set `GROUPME_BOT_AVATAR_PATH`, or leave both empty to use the Discord avatar.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    # GroupMe has no update endpoint, so the bot is replaced with an identical one.
    try:
        await groupme_destroy_bot(session, bridge["bot_id"])
    except GroupMeError as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Could not remove the old bot",
                description=f"`{error}`",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    created: dict = {}
    try:
        created = await groupme_create_bot(
            session, bridge["group_id"], GROUPME_BOT_NAME, callback_url(), avatar
        )
    except GroupMeError:
        log.exception("Recreating bot %s with an avatar failed", bridge["bot_id"])

    new_bot_id = str(created.get("bot_id") or "")
    note = ""
    if not new_bot_id:
        try:
            created = await groupme_create_bot(
                session, bridge["group_id"], GROUPME_BOT_NAME, callback_url(), ""
            )
            new_bot_id = str(created.get("bot_id") or "")
            note = "\nGroupMe refused the avatar, so the bot was recreated without one."
        except GroupMeError as error:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Bot recreation failed",
                    description=(
                        f"The old bot was deleted and the replacement failed: `{error}`\n"
                        "Run `/bridge set-bot` with a new bot id, or `/bridge add` again."
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

    save_bridge(
        bridge["conversation_id"],
        int(bridge["channel_id"]),
        new_bot_id,
        bridge["conversation_name"],
        conversation_type=bridge["conversation_type"],
        group_id=bridge["group_id"],
        group_name=bridge["group_name"],
        target_kind=bridge["target_kind"],
        parent_id=bridge["parent_id"],
        send_mode=bridge["send_mode"] or "bot",
    )
    await client.refresh_bot_names()
    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Avatar updated",
            description=(
                f"Group `{bridge['group_id']}` now posts with bot `{new_bot_id}`.\n"
                f"Avatar: {avatar}{note}"
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="test", description="Send a test message in both directions.")
async def bridge_test(interaction: discord.Interaction):
    if not await require_manager(interaction):
        return
    bridge = await bridge_for_target_or_reply(interaction)
    if bridge is None:
        return

    await interaction.response.defer(ephemeral=True)
    session = interaction.client.groupme_session
    problems: list[str] = []
    test_text = f"Catbird test message from Discord (#{getattr(interaction.channel, 'name', 'post')})."
    try:
        if bridge["send_mode"] == "user":
            await groupme_post_message(
                session, outbound_conversation_id(bridge), test_text
            )
        else:
            await groupme_post_bot(session, bridge["bot_id"], test_text)
    except GroupMeError as error:
        problems.append(f"GroupMe post failed: `{error}`")

    try:
        await interaction.client.send_via_webhook(
            bridge,
            content="Catbird test message from GroupMe.",
            username=WEBHOOK_NAME,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.HTTPException, RuntimeError) as error:
        problems.append(f"Discord webhook failed: `{error}`")

    if problems:
        embed = discord.Embed(
            title="⚠️ Test finished with problems",
            description="\n".join(problems),
            color=discord.Color.orange(),
        )
    else:
        embed = discord.Embed(
            title="✅ Test messages sent",
            description=(
                f"Check **{conversation_label(bridge)}** in GroupMe and "
                f"{destination_label(bridge)} here."
            ),
            color=discord.Color.green(),
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


privacy_group = app_commands.Group(
    name="privacy",
    description="Control how GroupMe display names are mirrored into Discord (all bridges).",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@privacy_group.command(
    name="set", description="Set how one GroupMe display name is mirrored into Discord."
)
@app_commands.describe(
    name="GroupMe display name exactly as it appears in the group",
    mode="share (default), anonymize (hide name and avatar), or opt out (do not mirror)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="share", value=PRIVACY_SHARE),
        app_commands.Choice(name="anonymize", value=PRIVACY_ANONYMIZE),
        app_commands.Choice(name="opt out", value=PRIVACY_OPTOUT),
    ]
)
async def privacy_set(
    interaction: discord.Interaction, name: str, mode: app_commands.Choice[str]
):
    if not await require_manager(interaction):
        return
    display = " ".join(name.split())
    if not display:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Missing display name",
                description="Pass the GroupMe display name as it appears in the group.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return
    set_groupme_user_mode(display, mode.value)
    escaped = discord.utils.escape_markdown(display)
    if mode.value == PRIVACY_SHARE:
        description = (
            f"**{escaped}** is mirrored with their GroupMe name and avatar again."
        )
    elif mode.value == PRIVACY_ANONYMIZE:
        description = (
            f"**{escaped}** is now posted as **{ANONYMOUS_GROUPME_NAME}** without "
            "their avatar."
        )
    else:
        description = f"Messages from **{escaped}** are no longer mirrored into Discord."
    description += (
        "\n\nThis applies to every bridge. Matching uses the GroupMe display name, "
        "so a renamed user needs the new name."
    )
    log.info("Privacy: %s set to %s", display, mode.value)
    await interaction.response.send_message(
        embed=discord.Embed(
            title="✅ Privacy setting updated",
            description=description,
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@privacy_group.command(
    name="list", description="Show which GroupMe display names are anonymized or opted out."
)
async def privacy_list(interaction: discord.Interaction):
    if not await require_manager(interaction):
        return
    rows = list_groupme_user_modes()
    anonymized = [r["name"] for r in rows if r["mode"] == PRIVACY_ANONYMIZE]
    opted_out = [r["name"] for r in rows if r["mode"] == PRIVACY_OPTOUT]
    embed = discord.Embed(title="GroupMe privacy settings", color=discord.Color.blurple())
    embed.add_field(
        name=f"Anonymized ({len(anonymized)})",
        value=truncate("\n".join(anonymized) or "None", 1024),
        inline=False,
    )
    embed.add_field(
        name=f"Opted out ({len(opted_out)})",
        value=truncate("\n".join(opted_out) or "None", 1024),
        inline=False,
    )
    embed.set_footer(text="Matched by GroupMe display name across every bridge.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Discord client
# --------------------------------------------------------------------------- #


class Bot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(bridge_group)
        self.tree.add_command(privacy_group)
        self.commands_synced = False
        self.groupme_session: aiohttp.ClientSession | None = None
        self.web_runner: web.AppRunner | None = None
        self.ngrok_listener = None
        self.webhooks: dict[int, discord.Webhook] = {}

    # -- lifecycle --------------------------------------------------------- #

    async def setup_hook(self) -> None:
        self.groupme_session = aiohttp.ClientSession()
        if GROUPME_MODE == "webhook":
            await self.start_webhook_server()

    async def close(self) -> None:
        if self.ngrok_listener is not None:
            listener, self.ngrok_listener = self.ngrok_listener, None
            try:
                result = listener.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("Could not close the ngrok tunnel")
        if self.web_runner is not None:
            await self.web_runner.cleanup()
            self.web_runner = None
        if self.groupme_session is not None and not self.groupme_session.closed:
            await self.groupme_session.close()
        await super().close()

    async def start_webhook_server(self) -> None:
        app = web.Application()
        app.router.add_post(GROUPME_WEBHOOK_PATH, self.handle_groupme_callback)
        app.router.add_get("/healthz", lambda _request: web.json_response({"ok": True}))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, GROUPME_WEBHOOK_HOST, GROUPME_WEBHOOK_PORT)
        await site.start()
        log.info(
            "GroupMe callback server listening on http://%s:%d%s",
            GROUPME_WEBHOOK_HOST,
            GROUPME_WEBHOOK_PORT,
            GROUPME_WEBHOOK_PATH,
        )
        await self.start_tunnel()

    async def start_tunnel(self) -> None:
        """Expose the callback server with ngrok and remember its public URL."""
        global PUBLIC_BASE_URL
        if not NGROK_ENABLED:
            if not PUBLIC_BASE_URL:
                log.warning(
                    "Webhook mode has no public URL: set NGROK_ENABLED=true or "
                    "GROUPME_PUBLIC_URL so GroupMe can reach port %d.",
                    GROUPME_WEBHOOK_PORT,
                )
            return
        try:
            import ngrok
        except ImportError:
            log.error(
                "NGROK_ENABLED is set but the 'ngrok' package is missing: "
                "pip install -r requirements.txt"
            )
            return
        options = {"authtoken_from_env": True}
        if NGROK_DOMAIN:
            options["domain"] = NGROK_DOMAIN
        try:
            listener = await ngrok.forward(
                f"localhost:{GROUPME_WEBHOOK_PORT}", **options
            )
        except Exception:
            log.exception(
                "Could not open the ngrok tunnel; is NGROK_AUTHTOKEN set and the "
                "reserved domain %r available?",
                NGROK_DOMAIN or "(ephemeral)",
            )
            return
        self.ngrok_listener = listener
        PUBLIC_BASE_URL = listener.url().rstrip("/")
        log.info("ngrok tunnel ready: %s%s", PUBLIC_BASE_URL, GROUPME_WEBHOOK_PATH)

    async def sync_commands(self) -> None:
        try:
            if DISCORD_GUILD_ID:
                await self.purge_global_commands()
                guild = discord.Object(id=int(DISCORD_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info(
                    "Application commands synced to guild %s (%s)",
                    DISCORD_GUILD_ID,
                    describe_commands(self.tree),
                )
            else:
                await self.tree.sync()
                log.info(
                    "Global application commands synced (%s)",
                    describe_commands(self.tree),
                )
            self.commands_synced = True
        except (discord.HTTPException, ValueError):
            # Retried on the next gateway READY, so a rate limit or a blip self-heals.
            log.exception("Could not sync application commands")

    async def purge_global_commands(self) -> None:
        """Delete globally registered commands left over from an earlier run.

        Guild-scoped commands are what this bot uses, but a global set from a run
        without `DISCORD_GUILD_ID` survives every later guild resync. Discord then
        lists both, and invoking the stale copy raises `CommandSignatureMismatch`
        in autocomplete callbacks (the parameters no longer match the code).
        """
        if self.application_id is None:
            return
        try:
            existing = await self.tree.fetch_commands(guild=None)
        except discord.HTTPException:
            log.warning("Could not check for stale global commands")
            return
        if not existing:
            return
        try:
            # An empty bulk upsert deletes every global command without touching the
            # local tree, which still holds the commands copied into the guild scope.
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except discord.HTTPException:
            log.warning("Could not delete stale global commands")
            return
        log.warning(
            "Removed %d stale global application command(s): %s",
            len(existing),
            ", ".join(sorted(command.name for command in existing)),
        )

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if not self.commands_synced:
            await self.sync_commands()

        await self.refresh_bot_names()

        if GROUPME_MODE not in {"webhook", "poll"}:
            log.error("Unknown GROUPME_MODE %r; expected 'poll' or 'webhook'", GROUPME_MODE)
            await self.close()
            return
        if GROUPME_MODE == "poll" and not GROUPME_ACCESS_TOKEN:
            log.error(
                "GROUPME_MODE=poll requires GROUPME_ACCESS_TOKEN. Set it in .env "
                "(or switch to GROUPME_MODE=webhook)."
            )
            await self.close()
            return

        if GROUPME_MODE == "webhook":
            url = callback_url()
            log.info(
                "GroupMe transport: webhook (bot callbacks)%s",
                f" -> {url}" if url else " (no public URL configured!)",
            )
        else:
            log.info("GroupMe transport: poll (every %ds)", GROUPME_POLL_SECONDS)

        targets = poll_targets()
        topic_count = sum(1 for _cid, bridge in targets if bridge["conversation_type"] == "topic")
        if targets and not GROUPME_ACCESS_TOKEN:
            log.error(
                "%d polled conversation(s) need GROUPME_ACCESS_TOKEN; they will not "
                "receive messages.",
                len(targets),
            )
        if targets:
            log.info(
                "Polling %d GroupMe conversation(s) every %ds (%d-message window, "
                "%d every %ds; %d topic(s), %d main chat(s)): %s",
                len(targets),
                GROUPME_POLL_SECONDS,
                GROUPME_POLL_RECENT_LIMIT,
                GROUPME_HISTORY_LIMIT,
                GROUPME_POLL_BACKFILL_EVERY * GROUPME_POLL_SECONDS,
                topic_count,
                len(targets) - topic_count,
                ", ".join(
                    f"{conversation_id} ({poll_reason(bridge)})"
                    for conversation_id, bridge in targets
                ),
            )

        # The loop is harmless when there is nothing to poll: topics are always polled,
        # main chats only when GROUPME_MODE=poll or the bridge has no bot.
        if not poll_groupme.is_running():
            poll_groupme.start()
        if SYNC_DELETIONS and not check_groupme_deletions.is_running():
            check_groupme_deletions.start()

        invite = discord.utils.oauth_url(self.user.id, permissions=REQUIRED_PERMS, scopes=("bot",))
        log.info("Bot invite URL (Manage Webhooks + message permissions): %s", invite)

    # -- GroupMe helpers ---------------------------------------------------- #

    async def host_avatar(self, url: str) -> str:
        """Download an image URL and re-host it on GroupMe (bot avatars need i.groupme.com)."""
        async with self.groupme_session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                raise GroupMeError(f"could not download {url} ({response.status})")
            data = await read_body_limited(response, IMAGE_SOURCE_LIMIT_BYTES)
            content_type = response.content_type or "image/png"
        if not data:
            raise GroupMeError(f"{url} returned an empty or oversized body")
        return await groupme_upload_image(self.groupme_session, data, content_type)

    async def refresh_bot_names(self) -> None:
        """Cache bot names so bot-authored messages are never mirrored back."""
        if not GROUPME_ACCESS_TOKEN:
            return
        try:
            bots = await groupme_list_bots(self.groupme_session)
        except GroupMeError:
            log.exception("Could not list GroupMe bots")
            return
        BOT_NAMES.clear()
        for bot in bots:
            name = (bot.get("name") or "").strip().casefold()
            if name:
                BOT_NAMES.add(name)
        log.debug("Cached %d GroupMe bot name(s)", len(BOT_NAMES))

    async def get_webhook(self, bridge: sqlite3.Row, *, refresh: bool = False) -> discord.Webhook:
        destination_id = int(bridge["channel_id"])
        if not refresh:
            cached = self.webhooks.get(destination_id)
            if cached is not None:
                return cached

        webhook: discord.Webhook | None = None
        if not refresh and bridge["webhook_id"] and bridge["webhook_token"]:
            try:
                webhook = discord.Webhook.partial(
                    int(bridge["webhook_id"]), bridge["webhook_token"], client=self
                )
                await webhook.fetch()
            except discord.HTTPException:
                log.warning(
                    "Webhook %s for destination %s is unusable; creating a replacement",
                    bridge["webhook_id"],
                    destination_id,
                )
                webhook = None

        if webhook is None:
            # Forum posts and threads have no webhook of their own: the webhook lives on
            # the parent channel and every post passes the thread id.
            owner_id = webhook_channel_id(bridge)
            channel = self.get_channel(owner_id) or await self.fetch_channel(owner_id)
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                raise RuntimeError(f"Channel {owner_id} cannot host a webhook")
            webhook = await channel.create_webhook(
                name=WEBHOOK_NAME, reason="Catbird GroupMe bridge"
            )
            save_webhook(bridge["conversation_id"], webhook.id, webhook.token)

        self.webhooks[destination_id] = webhook
        return webhook

    async def ensure_target_open(self, bridge: sqlite3.Row) -> None:
        """Unarchive a forum post or thread so messages can be posted into it."""
        if bridge["target_kind"] != "thread":
            return
        destination_id = int(bridge["channel_id"])
        try:
            thread = self.get_channel(destination_id) or await self.fetch_channel(destination_id)
            if isinstance(thread, discord.Thread) and thread.archived:
                await thread.edit(archived=False)
                log.info("Unarchived thread %s to relay GroupMe messages", destination_id)
        except discord.HTTPException:
            log.warning("Could not unarchive thread %s", destination_id)

    async def send_via_webhook(
        self, bridge: sqlite3.Row, *, refresh: bool = False, **payload
    ) -> discord.WebhookMessage:
        """Post with the bridge webhook, into the thread when the destination is a post."""
        await self.ensure_target_open(bridge)
        webhook = await self.get_webhook(bridge, refresh=refresh)
        if bridge["target_kind"] == "thread":
            payload["thread"] = discord.Object(id=int(bridge["channel_id"]))
        payload.setdefault("wait", True)
        return await webhook.send(**payload)

    # -- GroupMe ingestion -------------------------------------------------- #

    async def handle_groupme_callback(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        if LOG_GROUPME_PAYLOADS:
            log.info("GroupMe callback payload: %s", json.dumps(payload, ensure_ascii=False))
            append_payload_log(payload)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False}, status=400)
        # Acknowledge immediately; GroupMe retries slow callbacks.
        self.spawn_groupme_processing(payload)
        return web.json_response({"ok": True})

    def spawn_groupme_processing(self, payload: dict) -> None:
        """Ingest off the request task, logging failures instead of swallowing them."""
        task = asyncio.create_task(self.process_groupme_message(payload, source="webhook"))

        def report(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                log.error(
                    "GroupMe callback for message %s failed",
                    payload.get("id"),
                    exc_info=error,
                )

        task.add_done_callback(report)

    async def process_groupme_message(self, message: dict, *, source: str) -> None:
        group_id = str(message.get("group_id") or "")
        message_id = str(message.get("id") or "")
        if not group_id:
            log.info(
                "Ignoring GroupMe message %s via %s: payload has no group_id",
                message_id or "?",
                source,
            )
            return
        bridge = bridge_for_conversation(group_id)
        if bridge is None:
            log.info(
                "Ignoring GroupMe message %s from group %s via %s: conversation is not bridged",
                message_id or "?",
                group_id,
                source,
            )
            return
        deleted_id = groupme_deleted_message_id(message)
        if deleted_id:
            await self.delete_groupme_copy(bridge, deleted_id)
            return
        if is_groupme_bot_message(message):
            log.debug("Ignoring GroupMe bot/system message in %s via %s", group_id, source)
            return

        if not message_id:
            log.info(
                "Ignoring GroupMe message via %s: group %s payload has no id", source, group_id
            )
            return
        if message_id in RECENT_OUTBOUND:
            log.info("Skipping GroupMe message %s: posted by the bridge itself", message_id)
            return
        if link_exists(LINK_GROUPME, group_id, message_id):
            log.info(
                "Skipping already-relayed GroupMe message %s (group %s, via %s)",
                message_id,
                group_id,
                source,
            )
            return

        privacy = groupme_user_mode(message.get("name") or "")
        if privacy == PRIVACY_OPTOUT:
            log.info(
                "Skipping GroupMe message %s from group %s via %s: the display name "
                "opted out of Discord sharing",
                message_id,
                group_id,
                source,
            )
            return

        text = groupme_message_text(message)
        image_urls = groupme_image_urls(message)
        if not text and not image_urls:
            log.info(
                "Ignoring GroupMe message %s from group %s via %s: no text and no images",
                message_id,
                group_id,
                source,
            )
            return

        if privacy == PRIVACY_ANONYMIZE:
            username = ANONYMOUS_GROUPME_NAME
            avatar_url = None
        else:
            username = sanitize_webhook_name(message.get("name") or "GroupMe user")
            avatar_url = message.get("avatar_url") or None
        sent: discord.WebhookMessage | None = None
        files: list[discord.File] = []
        for attempt in (1, 2):
            try:
                files = await download_groupme_images(self.groupme_session, image_urls)
                if not text and not files:
                    log.warning(
                        "GroupMe message %s had images that could not be downloaded",
                        message_id,
                    )
                    return
                sent = await self.send_via_webhook(
                    bridge,
                    refresh=attempt == 2,
                    content=text or None,
                    username=username,
                    avatar_url=avatar_url,
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                break
            except (discord.NotFound, discord.Forbidden):
                # A deleted or orphaned webhook can be replaced once per message.
                self.webhooks.pop(int(bridge["channel_id"]), None)
                if attempt == 2:
                    log.exception("Could not relay GroupMe message %s", message_id)
                    return
                log.warning(
                    "Bridge webhook for group %s is unusable; retrying with a new webhook",
                    group_id,
                )
            except (discord.HTTPException, RuntimeError):
                log.exception("Could not relay GroupMe message %s", message_id)
                return

        add_link(
            LINK_GROUPME,
            group_id,
            int(bridge["channel_id"]),
            message_id,
            str(sent.id) if sent else None,
            from_epoch(message.get("created_at")),
        )
        set_last_seen(group_id, message_id)
        log.info(
            "Relayed GroupMe message %s from group %s to channel %s (%d image(s), via %s)",
            message_id,
            group_id,
            bridge["channel_id"],
            len(files),
            source,
        )

    # -- Discord events ----------------------------------------------------- #

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        bridge = bridge_for_destination(bridged_channel_id(message))
        if bridge is None:
            return
        try:
            await relay_discord_message(self.groupme_session, message, bridge, self)
        except (GroupMeError, discord.HTTPException):
            log.exception("Could not relay Discord message %s to GroupMe", message.id)

    async def on_raw_message_delete(self, payload) -> None:
        await self.sync_discord_deletion(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload) -> None:
        # A purge does not fire one event per message, so walk the ids ourselves.
        for message_id in payload.message_ids:
            await self.sync_discord_deletion(message_id)

    async def sync_discord_deletion(self, discord_id: int) -> None:
        """Delete the GroupMe copy of a Discord message that was removed."""
        if not SYNC_DELETIONS:
            return
        rows = outbound_messages(discord_id)
        if not rows:
            return
        if not GROUPME_ACCESS_TOKEN:
            log.warning(
                "Discord message %s was deleted, but GROUPME_ACCESS_TOKEN is unset; "
                "its GroupMe copy cannot be removed",
                discord_id,
            )
            return
        for row in rows:
            conversation_id = str(row["group_id"])
            groupme_id = str(row["groupme_id"])
            try:
                await groupme_delete_message(
                    self.groupme_session, conversation_id, groupme_id
                )
            except GroupMeError as error:
                log.warning(
                    "Could not delete GroupMe message %s in %s after Discord message %s "
                    "was deleted: %s",
                    groupme_id,
                    conversation_id,
                    discord_id,
                    error,
                )
                continue
            remove_outbound(conversation_id, discord_id, groupme_id)
            remove_link(LINK_GROUPME, conversation_id, groupme_id)
            log.info(
                "Deleted GroupMe message %s in %s because Discord message %s was deleted",
                groupme_id,
                conversation_id,
                discord_id,
            )

    async def delete_groupme_copy(self, bridge: sqlite3.Row, source_id: str) -> bool:
        """Delete the Discord copy of a GroupMe message that GroupMe removed."""
        conversation_id = str(bridge["conversation_id"])
        source_id = str(source_id)
        link = db.execute(
            """
            SELECT target_id FROM message_links
             WHERE direction=? AND group_id=? AND source_id=? AND target_id IS NOT NULL
            """,
            (LINK_GROUPME, conversation_id, source_id),
        ).fetchone()
        if link is None:
            return False
        if not await self.delete_discord_copy(bridge, str(link["target_id"])):
            return False
        remove_link(LINK_GROUPME, conversation_id, source_id)
        log.info(
            "Deleted the Discord copy of GroupMe message %s in %s: it was deleted",
            source_id,
            conversation_id,
        )
        return True

    async def delete_discord_copy(self, bridge: sqlite3.Row, discord_id: str) -> bool:
        """Remove the webhook message that mirrors a GroupMe message."""
        message_id = int(discord_id)
        thread = (
            discord.Object(id=int(bridge["channel_id"]))
            if bridge["target_kind"] == "thread"
            else None
        )
        try:
            webhook = await self.get_webhook(bridge)
            # `thread=None` would make discord.py read `.id` from None; omit it instead.
            if thread is None:
                await webhook.delete_message(message_id)
            else:
                await webhook.delete_message(message_id, thread=thread)
            return True
        except (discord.HTTPException, RuntimeError):
            # Includes the case where the webhook was replaced: the token no longer
            # owns the old copy, so the bot itself has to remove it.
            log.warning(
                "The bridge webhook could not delete Discord message %s; trying the bot token",
                message_id,
            )
        try:
            channel = self.get_channel(
                int(bridge["channel_id"])
            ) or await self.fetch_channel(int(bridge["channel_id"]))
            await channel.get_partial_message(message_id).delete()
            return True
        except discord.NotFound:
            return True
        except (discord.HTTPException, AttributeError):
            log.exception(
                "Could not delete Discord message %s mirroring a GroupMe message", message_id
            )
            return False

    async def reconcile_groupme_deletions(
        self, session: aiohttp.ClientSession, bridge: sqlite3.Row
    ) -> None:
        """Delete Discord copies of GroupMe messages that no longer exist.

        GroupMe emits no delete callback, so the newest page of the conversation is
        compared with the links this bridge recorded. Only linked messages inside
        that page can be checked; older ones are left alone rather than paged for.
        """
        conversation_id = str(bridge["conversation_id"])
        links = db.execute(
            """
            SELECT source_id, target_id FROM message_links
             WHERE direction=? AND group_id=? AND target_id IS NOT NULL
            """,
            (LINK_GROUPME, conversation_id),
        ).fetchall()
        if not links:
            return

        messages = await groupme_list_messages(
            session, conversation_id, limit=GROUPME_HISTORY_LIMIT
        )
        if not messages:
            return
        present = {
            str(message.get("id"))
            for message in messages
            if message.get("id") and not groupme_deleted_message_id(message)
        }
        complete = len(messages) < GROUPME_HISTORY_LIMIT
        oldest = oldest_numeric_id(messages)

        for link in links:
            source_id = str(link["source_id"])
            if source_id in present:
                continue
            # A page shorter than the limit covers all history; otherwise only ids
            # at or above its oldest entry can be proven absent.
            if not complete and not message_id_at_least(source_id, oldest):
                continue
            await self.delete_groupme_copy(bridge, source_id)


def poll_reason(bridge: sqlite3.Row) -> str:
    """Why `poll_targets` polls this bridge; used in the startup summary."""
    if bridge["conversation_type"] == "topic":
        return "topic"
    if not bridge["bot_id"]:
        return "no bot"
    return "poll mode"


def poll_targets() -> list[tuple[str, sqlite3.Row]]:
    """Conversations that need polling.

    Topics (subgroups) never produce callbacks, so a topic bridge is always polled.
    A main-chat bridge is polled when GROUPME_MODE=poll, and also when it has no bot
    (without a bot there are no callbacks either).
    """
    targets: list[tuple[str, sqlite3.Row]] = []
    for bridge in list_bridges():
        if (
            bridge["conversation_type"] == "topic"
            or GROUPME_MODE == "poll"
            or not bridge["bot_id"]
        ):
            targets.append((str(bridge["conversation_id"]), bridge))
    return targets


@tasks.loop(seconds=GROUPME_DELETE_CHECK_SECONDS)
async def check_groupme_deletions() -> None:
    """Remove Discord copies of GroupMe messages that were deleted in GroupMe."""
    if not SYNC_DELETIONS or not GROUPME_ACCESS_TOKEN:
        return
    async with aiohttp.ClientSession() as session:
        for bridge in list_bridges():
            conversation_id = str(bridge["conversation_id"])
            try:
                await client.reconcile_groupme_deletions(session, bridge)
            except GroupMeError:
                log.exception("Could not check %s for deleted messages", conversation_id)
            except aiohttp.ClientError:
                log.exception("GroupMe deletion check failed for %s", conversation_id)
            await asyncio.sleep(GROUPME_REQUEST_DELAY_SECONDS)


poll_cycles = 0


@tasks.loop(seconds=GROUPME_POLL_SECONDS)
async def poll_groupme() -> None:
    global poll_cycles
    poll_cycles += 1
    # Cheap ticks only ask for the newest few messages; every
    # GROUPME_POLL_BACKFILL_SECONDS the same loop fetches the wider window to catch
    # up after a restart or a quiet period.
    limit = (
        GROUPME_HISTORY_LIMIT
        if poll_cycles % GROUPME_POLL_BACKFILL_EVERY == 0
        else GROUPME_POLL_RECENT_LIMIT
    )
    targets = poll_targets()
    if not targets:
        return
    async with aiohttp.ClientSession() as session:
        for conversation_id, bridge in targets:
            try:
                await poll_conversation(session, conversation_id, bridge, limit=limit)
            except GroupMeError:
                log.exception("GroupMe poll failed for conversation %s", conversation_id)
            except aiohttp.ClientError:
                log.exception("GroupMe poll request failed for conversation %s", conversation_id)
            await asyncio.sleep(GROUPME_REQUEST_DELAY_SECONDS)


async def newest_message_id(
    session: aiohttp.ClientSession, conversation_id: str, bridge: sqlite3.Row
) -> str:
    """Newest message id for a group's main chat or one of its topics, for baselining."""
    if bridge["conversation_type"] == "topic":
        for topic in await groupme_list_subgroups(session, bridge["group_id"]):
            if str(topic.get("id")) == str(conversation_id):
                return str((topic.get("messages") or {}).get("last_message_id") or "")
        return ""
    group = await groupme_get_group(session, conversation_id)
    return str(((group or {}).get("messages") or {}).get("last_message_id") or "")


async def poll_conversation(
    session: aiohttp.ClientSession,
    conversation_id: str,
    bridge: sqlite3.Row,
    *,
    limit: int = GROUPME_POLL_RECENT_LIMIT,
) -> None:
    since_id = last_seen(conversation_id)
    if not since_id:
        baseline = await newest_message_id(session, conversation_id, bridge)
        if baseline:
            set_last_seen(conversation_id, baseline)
            log.info(
                "Baselined conversation %s at message %s (history is not replayed)",
                conversation_id,
                baseline,
            )
        return

    if not GROUPME_ACCESS_TOKEN:
        return

    messages = await groupme_list_messages(
        session, conversation_id, since_id=since_id, limit=limit
    )
    if not messages:
        return
    # A full short window may hide older messages behind it, so widen before the
    # cursor advances; otherwise a burst would silently skip the middle of it.
    if len(messages) >= limit and limit < GROUPME_HISTORY_LIMIT:
        messages = await groupme_list_messages(
            session, conversation_id, since_id=since_id, limit=GROUPME_HISTORY_LIMIT
        )
        if not messages:
            return
    if len(messages) >= GROUPME_HISTORY_LIMIT:
        log.warning(
            "Conversation %s has at least %d new messages; anything older than that "
            "window is skipped",
            conversation_id,
            GROUPME_HISTORY_LIMIT,
        )

    # The API returns newest first; relay oldest first so Discord keeps the order.
    for message in reversed(messages):
        await client.process_groupme_message(message, source="poll")

    newest = messages[0].get("id")
    if newest:
        set_last_seen(conversation_id, str(newest))


intents = discord.Intents.default()
intents.message_content = True
client = Bot(intents=intents)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE_LOGGING else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        # log_handler=None keeps discord.py from adding its own handler, so every line
        # is formatted once by basicConfig instead of appearing twice.
        client.run(DISCORD_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord rejected the login: enable MESSAGE CONTENT INTENT under "
            "https://discord.com/developers/applications -> your app -> Bot -> "
            "Privileged Gateway Intents, then start the bot again."
        )
        raise SystemExit(1)
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_TOKEN; copy a fresh bot token into .env.")
        raise SystemExit(1)
