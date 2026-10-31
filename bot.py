"""Catbird: a bidirectional GroupMe <-> Discord bridge.

Each bridged GroupMe conversation is mirrored into exactly one Discord channel
and vice versa. Bridges are created, changed, and removed with the `/bridge`
slash command and persisted in SQLite.

Message flow:
  Discord -> GroupMe  group bridges post through the group's GroupMe bot;
                      topic bridges post into the topic (subgroup) with the
                      access token because GroupMe does not allow bots there.
  GroupMe -> Discord  messages are posted through a channel webhook so the
                      original sender name and avatar are preserved.

A GroupMe "topic" is a subgroup with its own message stream. A topic bridge
relays the group's main chat *and* the mapped topic into one Discord channel,
while Discord messages go to the topic.

Edits are not mirrored: GroupMe's public API has no edit or delete endpoint.
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
GROUPME_REQUEST_DELAY_SECONDS = float(os.environ.get("GROUPME_REQUEST_DELAY_SECONDS", "1"))
GROUPME_WEBHOOK_HOST = os.environ.get("GROUPME_WEBHOOK_HOST", "0.0.0.0")
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
SCHEMA_VERSION = 1
GROUPME_TEXT_LIMIT = 1000
GROUPME_HISTORY_LIMIT = 100
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
LINK_GROUPME = "groupme_to_discord"
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


# GroupMe does not document topics. These are the fields a topic-aware payload is
# most likely to carry; `groupme_topic_of` degrades to "" when none are present.
TOPIC_KEYS = ("topic_id", "topic", "topic_name", "group_topic_id", "subgroup_id")


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


def groupme_topic_of(message: dict) -> str:
    """Return the topic marker of a GroupMe message, or "" when GroupMe sends none."""
    for key in TOPIC_KEYS:
        value = message.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("name") or value.get("topic_id")
        if value:
            return str(value).strip()
    return ""


# --------------------------------------------------------------------------- #
# Local state
# --------------------------------------------------------------------------- #

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
    db.executescript(
        """
        DROP TABLE IF EXISTS bridges;
        DROP TABLE IF EXISTS group_state;
        DROP TABLE IF EXISTS message_links;
        """
    )
db.executescript(
    f"""
    -- One GroupMe conversation <-> one Discord channel.
    -- group_id is the parent group; topic_id is set for topic (subgroup) bridges.
    CREATE TABLE IF NOT EXISTS bridges (
        group_id TEXT PRIMARY KEY,
        channel_id INTEGER NOT NULL UNIQUE,
        bot_id TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT '',
        webhook_id INTEGER,
        webhook_token TEXT,
        topic_id TEXT NOT NULL DEFAULT '',
        topic_name TEXT NOT NULL DEFAULT '',
        send_mode TEXT NOT NULL DEFAULT 'bot',
        created_at TEXT NOT NULL
    );
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
    PRAGMA user_version = {SCHEMA_VERSION};
    """
)
db.commit()


def migrate_bridges() -> None:
    """Add topic columns to databases created before topic bridges existed."""
    existing = {row["name"] for row in db.execute("PRAGMA table_info(bridges)")}
    for name, ddl in (
        ("topic_id", "TEXT NOT NULL DEFAULT ''"),
        ("topic_name", "TEXT NOT NULL DEFAULT ''"),
        ("send_mode", "TEXT NOT NULL DEFAULT 'bot'"),
    ):
        if name in existing:
            continue
        db.execute(f"ALTER TABLE bridges ADD COLUMN {name} {ddl}")
        log.info("Migrated bridges table: added %s", name)
    db.commit()


migrate_bridges()


def bridge_for_group(group_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM bridges WHERE group_id=?", (str(group_id),)).fetchone()


def bridge_for_conversation(conversation_id: str) -> sqlite3.Row | None:
    """Look up a bridge by topic (subgroup) id, then by parent group id."""
    conversation_id = str(conversation_id)
    row = db.execute(
        "SELECT * FROM bridges WHERE topic_id=? AND topic_id<>''", (conversation_id,)
    ).fetchone()
    if row is not None:
        return row
    return bridge_for_group(conversation_id)


def bridge_for_channel(channel_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM bridges WHERE channel_id=?", (int(channel_id),)).fetchone()


def list_bridges() -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM bridges ORDER BY group_name COLLATE NOCASE").fetchall()


def outbound_conversation_id(bridge: sqlite3.Row) -> str:
    """Where Discord messages for this bridge are posted."""
    topic_id = bridge["topic_id"] if "topic_id" in bridge.keys() else ""
    return str(topic_id or bridge["group_id"])


def save_bridge(
    group_id: str,
    channel_id: int,
    bot_id: str,
    group_name: str,
    webhook_id: int | None = None,
    webhook_token: str | None = None,
    topic_id: str = "",
    topic_name: str = "",
    send_mode: str = "bot",
) -> None:
    db.execute(
        """
        INSERT INTO bridges
            (group_id, channel_id, bot_id, group_name, webhook_id, webhook_token,
             topic_id, topic_name, send_mode, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET
            channel_id=excluded.channel_id,
            bot_id=excluded.bot_id,
            group_name=excluded.group_name,
            webhook_id=COALESCE(excluded.webhook_id, bridges.webhook_id),
            webhook_token=COALESCE(excluded.webhook_token, bridges.webhook_token),
            topic_id=excluded.topic_id,
            topic_name=excluded.topic_name,
            send_mode=excluded.send_mode
        """,
        (
            str(group_id),
            int(channel_id),
            bot_id,
            group_name,
            webhook_id,
            webhook_token,
            str(topic_id or ""),
            topic_name,
            send_mode,
            iso(utcnow()),
        ),
    )
    db.commit()


def save_webhook(group_id: str, webhook_id: int, webhook_token: str) -> None:
    db.execute(
        "UPDATE bridges SET webhook_id=?, webhook_token=? WHERE group_id=?",
        (int(webhook_id), webhook_token, str(group_id)),
    )
    db.commit()


def delete_bridge(group_id: str) -> None:
    bridge = bridge_for_group(group_id)
    db.execute("DELETE FROM bridges WHERE group_id=?", (str(group_id),))
    db.execute("DELETE FROM group_state WHERE group_id=?", (str(group_id),))
    db.execute("DELETE FROM message_links WHERE group_id=?", (str(group_id),))
    if bridge is not None and bridge["topic_id"]:
        topic_id = str(bridge["topic_id"])
        db.execute("DELETE FROM group_state WHERE group_id=?", (topic_id,))
        db.execute("DELETE FROM message_links WHERE group_id=?", (topic_id,))
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


# --------------------------------------------------------------------------- #
# GroupMe API
# --------------------------------------------------------------------------- #


class GroupMeError(Exception):
    """A GroupMe API call failed or exhausted its retries."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def groupme_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
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


async def groupme_list_bots(session: aiohttp.ClientSession) -> list[dict]:
    return await groupme_request(session, "GET", "/bots") or []


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


def groupme_message_text(message: dict) -> str:
    parts: list[str] = []
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
        elif kind not in {"image", "emoji"}:
            parts.append(f"[{kind or 'attachment'}]")
    return truncate("\n".join(parts).strip())


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
    """Thread messages are bridged through their parent channel's bridge."""
    channel = message.channel
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return channel.parent_id
    return channel.id


async def relay_discord_message(
    session: aiohttp.ClientSession, message: discord.Message, bridge: sqlite3.Row
) -> None:
    name = message.author.display_name or message.author.name
    text = render_discord_content(message)
    images = [a for a in message.attachments if is_image_attachment(a)]
    links = [a.url for a in message.attachments if not is_image_attachment(a)]

    body = f"{name}: {text}".rstrip()
    quote = quoted_reply(message)
    if quote:
        body = f"{quote}{body}"
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
        await send_to_topic(session, bridge, body, hosted)
    else:
        bot_id = bridge["bot_id"]
        await groupme_post_bot(session, bot_id, body)
        for image_url in hosted:
            await groupme_post_bot(session, bot_id, "", picture_url=image_url)

    log.info(
        "Relayed Discord message %s from #%s to GroupMe conversation %s (%d image(s), %d failed)",
        message.id,
        bridge["channel_id"],
        outbound_conversation_id(bridge),
        len(hosted),
        failed,
    )


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


async def send_to_topic(
    session: aiohttp.ClientSession,
    bridge: sqlite3.Row,
    body: str,
    image_urls: list[str],
) -> None:
    """Post into a topic (subgroup); GroupMe only allows this with the user token."""
    if not GROUPME_ACCESS_TOKEN:
        raise GroupMeError(
            "GROUPME_ACCESS_TOKEN is required to post into a GroupMe topic"
        )
    attachments = [{"type": "image", "url": url} for url in image_urls]

    conversation_id = outbound_conversation_id(bridge)
    created = await groupme_post_message(session, conversation_id, body, attachments)
    if created and created.get("id"):
        # Remember the id so the poller does not mirror our own topic post back to Discord.
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


def callback_url() -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}{GROUPME_WEBHOOK_PATH}"


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


async def bridge_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = (current or "").lower()
    choices = []
    for bridge in list_bridges():
        label = f"{bridge['group_name'] or 'Unnamed group'} ({bridge['group_id']})"
        if current in label.lower() or current in bridge["group_id"]:
            choices.append(app_commands.Choice(name=label[:100], value=bridge["group_id"]))
    return choices[:25]


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
    description="Bridge a GroupMe group, or a single topic inside it, to a Discord channel.",
)
@app_commands.describe(
    group_id="GroupMe group id (or a groupme.com join link)",
    channel="Discord text channel that mirrors the conversation",
    bot_id="GroupMe bot id for group bridges (create one with utils/groupme_setup.py)",
    topic="Optional GroupMe topic name or id: Discord posts go to this topic",
)
async def bridge_add(
    interaction: discord.Interaction,
    group_id: str,
    channel: discord.TextChannel,
    bot_id: str = "",
    topic: str = "",
):
    if not await require_manager(interaction):
        return
    group_id = parse_group_id(group_id)
    bot_id = (bot_id or "").strip()
    topic_query = (topic or "").strip()

    if not topic_query and not bot_id:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Missing GroupMe bot",
                description=(
                    "A group bridge needs a `bot_id`. A topic bridge can leave it out, "
                    "but then `topic` is required."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    existing_channel = bridge_for_channel(channel.id)
    if existing_channel is not None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Channel already bridged",
                description=(
                    f"{channel.mention} is already bridged to GroupMe group "
                    f"`{existing_channel['group_id']}`. Remove that bridge first."
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

    try:
        webhook = await channel.create_webhook(name=WEBHOOK_NAME, reason="Catbird GroupMe bridge")
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
    except discord.HTTPException as error:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Could not create webhook",
                description=f"Discord rejected the webhook request: `{error}`",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    group_name = (group or {}).get("name") or ""
    topic_id = str(topic_row.get("id")) if topic_row else ""
    topic_name = (topic_row.get("topic") or "") if topic_row else ""
    send_mode = "user" if topic_row else "bot"

    moved_from = None
    existing_group = bridge_for_group(group_id)
    if existing_group is not None and int(existing_group["channel_id"]) != channel.id:
        moved_from = int(existing_group["channel_id"])
        interaction.client.webhooks.pop(moved_from, None)
        if existing_group["webhook_id"]:
            try:
                old_webhook = await interaction.client.fetch_webhook(
                    int(existing_group["webhook_id"])
                )
                await old_webhook.delete(reason="GroupMe bridge moved to another channel")
            except discord.HTTPException:
                log.warning(
                    "Could not delete webhook %s left behind by the move",
                    existing_group["webhook_id"],
                )

    save_bridge(
        group_id,
        channel.id,
        bot_id,
        group_name,
        webhook.id,
        webhook.token,
        topic_id=topic_id,
        topic_name=topic_name,
        send_mode=send_mode,
    )
    interaction.client.webhooks[channel.id] = webhook

    last_message_id = ((group or {}).get("messages") or {}).get("last_message_id")
    if last_message_id:
        set_last_seen(group_id, str(last_message_id))
    else:
        warnings.append("No message baseline was recorded; existing GroupMe history may be replayed.")

    if topic_row:
        topic_last = ((topic_row.get("messages") or {}).get("last_message_id"))
        if topic_last:
            set_last_seen(topic_id, str(topic_last))
        else:
            warnings.append(
                f"No baseline for topic **{topic_name or topic_id}**; its history may be replayed."
            )

    await interaction.client.refresh_bot_names()

    if topic_row:
        target = f"topic **{topic_name or topic_id}** in group **{group_name or group_id}**"
    else:
        target = f"GroupMe group **{group_name or group_id}**"
    description = f"{channel.mention} now mirrors {target}."
    if topic_row:
        description += (
            "\nThe group's main chat and this topic are both relayed here; "
            "Discord messages are posted into the topic."
        )
    if moved_from:
        description += f"\nThe bridge moved from <#{moved_from}>."
    if warnings:
        description += "\n\n" + "\n".join(f"⚠️ {warning}" for warning in warnings)
    if GROUPME_MODE == "webhook":
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
    description="Send Discord messages to a different GroupMe topic, or back to the main chat.",
)
@app_commands.autocomplete(group_id=bridge_autocomplete)
@app_commands.describe(
    group_id="GroupMe group id",
    topic="Topic name or id (use 'none' to clear and post to the main chat)",
)
async def bridge_set_topic(interaction: discord.Interaction, group_id: str, topic: str):
    if not await require_manager(interaction):
        return
    bridge = bridge_for_group(parse_group_id(group_id))
    if bridge is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ No such bridge",
                description=f"No bridge is registered for GroupMe group `{group_id}`.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    channel_id = int(bridge["channel_id"])
    topic_query = (topic or "").strip()
    if topic_query.casefold() in {"", "none", "clear", "off"}:
        if not bridge["bot_id"]:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Bot id needed",
                    description=(
                        "This bridge has no GroupMe bot, so it cannot post to the main chat. "
                        "Create a bot with `utils/groupme_setup.py` and run "
                        "`/bridge set-bot` first."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        save_bridge(
            bridge["group_id"],
            channel_id,
            bridge["bot_id"],
            bridge["group_name"],
            topic_id="",
            topic_name="",
            send_mode="bot",
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Topic cleared",
                description=f"Discord messages now post to the main chat of **{bridge['group_name'] or bridge['group_id']}**.",
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
    save_bridge(
        bridge["group_id"],
        channel_id,
        bridge["bot_id"],
        bridge["group_name"],
        topic_id=topic_id,
        topic_name=topic_name,
        send_mode="user",
    )
    topic_last = ((topic_row.get("messages") or {}).get("last_message_id"))
    if topic_last:
        set_last_seen(topic_id, str(topic_last))

    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Topic updated",
            description=(
                f"Discord messages in <#{channel_id}> now post into topic "
                f"**{topic_name}** (`{topic_id}`).\n"
                "GroupMe messages from the main chat and this topic are both relayed here."
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="remove", description="Remove a GroupMe group bridge.")
@app_commands.autocomplete(group_id=bridge_autocomplete)
@app_commands.describe(group_id="GroupMe group id to unbridge")
async def bridge_remove(interaction: discord.Interaction, group_id: str):
    if not await require_manager(interaction):
        return
    bridge = bridge_for_group(parse_group_id(group_id))
    if bridge is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ No such bridge",
                description=f"No bridge is registered for GroupMe group `{group_id}`.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    interaction.client.webhooks.pop(int(bridge["channel_id"]), None)
    if bridge["webhook_id"]:
        try:
            webhook = await interaction.client.fetch_webhook(int(bridge["webhook_id"]))
            await webhook.delete(reason="GroupMe bridge removed")
        except (discord.NotFound, discord.Forbidden):
            log.warning("Could not delete webhook %s while removing %s", bridge["webhook_id"], bridge["group_id"])
    delete_bridge(bridge["group_id"])
    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Bridge removed",
            description=(
                f"Stopped mirroring GroupMe group **{bridge['group_name'] or bridge['group_id']}** "
                f"to <#{bridge['channel_id']}>."
            ),
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


@bridge_group.command(name="set-bot", description="Change the GroupMe bot used by a bridge.")
@app_commands.autocomplete(group_id=bridge_autocomplete)
@app_commands.describe(group_id="GroupMe group id", bot_id="New GroupMe bot id")
async def bridge_set_bot(interaction: discord.Interaction, group_id: str, bot_id: str):
    if not await require_manager(interaction):
        return
    bridge = bridge_for_group(parse_group_id(group_id))
    if bridge is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ No such bridge",
                description=f"No bridge is registered for GroupMe group `{group_id}`.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return
    save_bridge(
        bridge["group_id"],
        int(bridge["channel_id"]),
        bot_id.strip(),
        bridge["group_name"],
        topic_id=bridge["topic_id"],
        topic_name=bridge["topic_name"],
        send_mode=bridge["send_mode"],
    )
    await interaction.client.refresh_bot_names()
    await interaction.response.send_message(
        embed=discord.Embed(
            title="✅ Bot updated",
            description=f"Group `{bridge['group_id']}` now posts with bot `{bot_id.strip()}`.",
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
        embed.description = "No bridges yet. Use `/bridge add` to create one."
    for bridge in bridges[:25]:
        target = f"group `{bridge['group_id']}`"
        if bridge["topic_id"]:
            target = f"topic **{bridge['topic_name'] or bridge['topic_id']}** (`{bridge['topic_id']}`)\nof group `{bridge['group_id']}`"
        poster = "access token" if bridge["send_mode"] == "user" else f"bot `{bridge['bot_id']}`"
        embed.add_field(
            name=truncate(bridge["group_name"] or bridge["group_id"], 100),
            value=f"<#{bridge['channel_id']}>\n{target}\nposts via {poster}",
            inline=True,
        )
    embed.set_footer(text=f"{len(bridges)} bridge(s) · {GROUPME_MODE} mode")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bridge_group.command(name="test", description="Send a test message in both directions.")
@app_commands.autocomplete(group_id=bridge_autocomplete)
@app_commands.describe(group_id="GroupMe group id to test")
async def bridge_test(interaction: discord.Interaction, group_id: str):
    if not await require_manager(interaction):
        return
    bridge = bridge_for_group(parse_group_id(group_id))
    if bridge is None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ No such bridge",
                description=f"No bridge is registered for GroupMe group `{group_id}`.",
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    session = interaction.client.groupme_session
    problems: list[str] = []
    test_text = f"Catbird test message from Discord (#{interaction.channel.name})."
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
        webhook = await interaction.client.get_webhook(bridge, refresh=True)
        await webhook.send(
            content="Catbird test message from GroupMe.",
            username=WEBHOOK_NAME,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
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
        where = (
            f"topic **{bridge['topic_name'] or bridge['topic_id']}**"
            if bridge["topic_id"]
            else "the GroupMe group"
        )
        embed = discord.Embed(
            title="✅ Test messages sent",
            description=f"Check {where} and this channel.",
            color=discord.Color.green(),
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Discord client
# --------------------------------------------------------------------------- #


class Bot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(bridge_group)
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
                guild = discord.Object(id=int(DISCORD_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Application commands synced to guild %s", DISCORD_GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Global application commands synced")
            self.commands_synced = True
        except (discord.HTTPException, ValueError):
            log.exception("Could not sync application commands")

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

        topic_bridges = [bridge for bridge in list_bridges() if bridge["topic_id"]]
        if topic_bridges and not GROUPME_ACCESS_TOKEN:
            log.error(
                "%d topic bridge(s) need GROUPME_ACCESS_TOKEN; they will not receive messages.",
                len(topic_bridges),
            )
        if topic_bridges:
            log.info(
                "Polling %d topic conversation(s) every %ds (topics have no callbacks)",
                len(topic_bridges),
                GROUPME_POLL_SECONDS,
            )

        # The loop is harmless when there is nothing to poll: topic bridges are
        # polled in both transports, group bridges only when GROUPME_MODE=poll.
        if not poll_groupme.is_running():
            poll_groupme.start()

        invite = discord.utils.oauth_url(self.user.id, permissions=REQUIRED_PERMS, scopes=("bot",))
        log.info("Bot invite URL (Manage Webhooks + message permissions): %s", invite)

    # -- GroupMe helpers ---------------------------------------------------- #

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
        channel_id = int(bridge["channel_id"])
        if not refresh:
            cached = self.webhooks.get(channel_id)
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
                    "Webhook %s for channel %s is unusable; creating a replacement",
                    bridge["webhook_id"],
                    channel_id,
                )
                webhook = None

        if webhook is None:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError(f"Channel {channel_id} is not a text channel")
            webhook = await channel.create_webhook(
                name=WEBHOOK_NAME, reason="Catbird GroupMe bridge"
            )
            save_webhook(bridge["group_id"], webhook.id, webhook.token)

        self.webhooks[channel_id] = webhook
        return webhook

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
        asyncio.create_task(self.process_groupme_message(payload, source="webhook"))
        return web.json_response({"ok": True})

    async def process_groupme_message(self, message: dict, *, source: str) -> None:
        group_id = str(message.get("group_id") or "")
        if not group_id:
            return
        bridge = bridge_for_conversation(group_id)
        if bridge is None:
            log.debug("Ignoring GroupMe message for unbridged conversation %s", group_id)
            return
        if is_groupme_bot_message(message):
            log.debug("Ignoring GroupMe bot/system message in %s via %s", group_id, source)
            return

        message_id = str(message.get("id") or "")
        if not message_id:
            return
        if message_id in RECENT_OUTBOUND:
            log.debug("Skipping GroupMe message %s posted by the bridge", message_id)
            return
        if link_exists(LINK_GROUPME, group_id, message_id):
            log.debug("Skipping already-relayed GroupMe message %s", message_id)
            return

        text = groupme_message_text(message)
        image_urls = groupme_image_urls(message)
        if not text and not image_urls:
            return

        username = sanitize_webhook_name(message.get("name") or "GroupMe user")
        avatar_url = message.get("avatar_url") or None
        sent: discord.WebhookMessage | None = None
        files: list[discord.File] = []
        for attempt in (1, 2):
            try:
                webhook = await self.get_webhook(bridge, refresh=attempt == 2)
                files = await download_groupme_images(self.groupme_session, image_urls)
                if not text and not files:
                    log.warning(
                        "GroupMe message %s had images that could not be downloaded",
                        message_id,
                    )
                    return
                sent = await webhook.send(
                    content=text or None,
                    username=username,
                    avatar_url=avatar_url,
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                    wait=True,
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
            "Relayed GroupMe message %s from group %s to channel %s (%d image(s))",
            message_id,
            group_id,
            bridge["channel_id"],
            len(files),
        )

    # -- Discord events ----------------------------------------------------- #

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        bridge = bridge_for_channel(bridged_channel_id(message))
        if bridge is None:
            return
        try:
            await relay_discord_message(self.groupme_session, message, bridge)
        except (GroupMeError, discord.HTTPException):
            log.exception("Could not relay Discord message %s to GroupMe", message.id)


def poll_targets() -> list[tuple[str, sqlite3.Row]]:
    """Conversations that need polling.

    Topic conversations never produce callbacks, so they are polled in both
    transports. Parent groups are only polled when GROUPME_MODE=poll.
    """
    targets: list[tuple[str, sqlite3.Row]] = []
    for bridge in list_bridges():
        topic_id = bridge["topic_id"] or ""
        if topic_id:
            targets.append((str(topic_id), bridge))
        elif GROUPME_MODE == "poll":
            targets.append((str(bridge["group_id"]), bridge))
    return targets


@tasks.loop(seconds=GROUPME_POLL_SECONDS)
async def poll_groupme() -> None:
    targets = poll_targets()
    if not targets:
        return
    async with aiohttp.ClientSession() as session:
        for conversation_id, bridge in targets:
            try:
                await poll_conversation(session, conversation_id, bridge)
            except GroupMeError:
                log.exception("GroupMe poll failed for conversation %s", conversation_id)
            except aiohttp.ClientError:
                log.exception("GroupMe poll request failed for conversation %s", conversation_id)
            await asyncio.sleep(GROUPME_REQUEST_DELAY_SECONDS)


async def newest_message_id(
    session: aiohttp.ClientSession, conversation_id: str, bridge: sqlite3.Row
) -> str:
    """Newest message id for a group or one of its topics, used to baseline polling."""
    if conversation_id == str(bridge["group_id"]):
        group = await groupme_get_group(session, conversation_id)
        return str(((group or {}).get("messages") or {}).get("last_message_id") or "")
    for topic in await groupme_list_subgroups(session, bridge["group_id"]):
        if str(topic.get("id")) == conversation_id:
            return str((topic.get("messages") or {}).get("last_message_id") or "")
    return ""


async def poll_conversation(
    session: aiohttp.ClientSession, conversation_id: str, bridge: sqlite3.Row
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

    messages = await groupme_list_messages(session, conversation_id, since_id=since_id)
    if not messages:
        return

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
        client.run(DISCORD_TOKEN)
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
