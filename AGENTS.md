# Project guidance

`bot.py` is a single-file bidirectional GroupMe <-> Discord bridge. Everything below is
a constraint that has already bitten us once.

## Model

- **Strictly one-to-one.** Each GroupMe conversation (a group's main chat, or one of its
  topics) maps to exactly one Discord destination, and each destination carries exactly
  one bridge: `bridges.conversation_id` is the primary key and `bridges.channel_id` is
  unique. A group may have a main-chat bridge and a topic bridge at once, on different
  destinations.
- **Destination comes from context.** `/bridge add` uses the channel/post it runs in: a
  text channel, a forum channel (`forum.create_thread(...)` then bridge the post), or an
  existing post/thread. Every other `/bridge` command except `topics` acts on the current
  channel/post, so none take a Discord channel argument.
- **`SCHEMA_VERSION` rebuilds the tables** when it changes (`init_db` drops and
  recreates them). There is no migration path; bump it only when losing bridges is
  intended. Keep the single-file structure.

## GroupMe specifics

- **Topics are subgroups**: `GET /groups/{id}/subgroups` → `id`, `parent_id`, `topic`.
  They have no bots and never fire callbacks. A topic bridge stores
  `conversation_type='topic'` and `send_mode='user'`, is polled every cycle, and posts
  with `POST /groups/{topic_id}/messages`.
- **Polling windows**: most cycles fetch `GROUPME_POLL_RECENT_LIMIT` (5) messages;
  every `GROUPME_POLL_BACKFILL_SECONDS` (300) a cycle fetches `GROUPME_HISTORY_LIMIT`
  (25) instead, and a full short window immediately retries wider so a burst is not
  skipped. `GROUPME_HISTORY_LIMIT` is also the window used to detect deletions.
- **Posting**: `send_mode='bot'` → `/bots/post`; `send_mode='user'` → `send_as_user()`
  (access token plus `image.groupme.com` uploads), used for topics *and* for groups that
  refuse bots. `/bridge set-mode` can also switch a main-chat bridge to `user` while
  keeping its bot for callbacks: token posts return their id, so deletion sync is exact,
  whereas bot posts are owned by the bot and some groups answer `403` when the token's
  account tries to delete them.
- **Privacy is bridge-wide and keyed by display name**: `groupme_user_privacy` stores
  a casefolded GroupMe display name with `share`/`anonymize`/`optout`. Never key it on a
  user id — a renamed user needs a new row. `optout` returns before images are
  downloaded; `anonymize` posts as `GroupMe user` with no avatar. Replies usually
  carry neither `name` nor `text` (rendered `> (GroupMe reply)`); when a quote does
  include a name, an anonymized one becomes `GroupMe user` and an opted-out one drops
  both name and text. `/privacy set` and `/privacy list` are administrator-only.
- **Groups can forbid bots**: `POST /bots` answers `401 unauthorized` for them, even in
  GroupMe's own UI. `/bridge add` must keep falling back to `send_mode='user'` plus
  polling — it is the only way to bridge such a group.
- **Bot creation** (`ensure_group_bot`): create automatically with `GROUPME_BOT_NAME`.
  GroupMe answers `400 ... already registered for group` when the callback URL is taken,
  so reuse the bot the account lists. `groupme_create_bot_with_fallback` retries without
  the avatar when GroupMe refuses it. The returned note carries GroupMe's own reason and
  **must stay in the user-facing failure reply**; hiding it made failures undiagnosable.
- **Avatars** must be `i.groupme.com` URLs; anything else is dropped. Upload a fresh copy
  per bot (`fresh_bot_avatar`) and never reuse one — GroupMe deletes the hosted image when
  its bot is destroyed. There is no bot update endpoint, so `/bridge set-avatar` deletes
  and recreates the bot.
- **`/bots/post` returns 404** for an unknown bot id: `recover_bot_id` finds or creates
  the group's bot, stores the id, and the relay retries once. `callback_url()` falls back
  to `NGROK_DOMAIN` so scripts outside the bot process still get a valid callback.
- **`groupme_request`** owns pacing and retries (429 + 5xx backoff, `304` means no new
  messages). Do not bypass it.
- **Edits have no endpoint**; never add code that pretends otherwise. Deletion uses
  the undocumented-but-stable `DELETE /conversations/:conversation_id/messages/:message_id`
  (204, 404 = already gone). GroupMe never drops a deleted message from the index: it
  keeps a tombstone with `deleted_at` and posts a `system` event with
  `event.type == "message.deleted"` naming the original id. Handle both, and keep the
  newest-`<GROUPME_HISTORY_LIMIT>` comparison as the fallback.
- **Deletion links**: `outbound_messages` maps one Discord id to every GroupMe id it
  produced (a bot message becomes one text post plus one picture post per image, and
  `/bots/post` returns no id, so `record_bot_posts` matches them by text and image URL).
  `message_links` rows with `target_id IS NULL` are the bridge's own token posts and must
  stay out of the reconciliation that deletes Discord copies.

## Discord specifics

- **Forum posts and threads have no webhook of their own**: `webhook_channel_id()` returns
  the parent, `send_via_webhook()` passes `thread=` when `target_kind='thread'`, and
  `ensure_target_open()` unarchives an archived post before posting.
- **Keep both idempotency guards**: Discord skips `author.bot` and `webhook_id`; GroupMe
  skips `system`, non-`user` `sender_type`, missing `user_id`, and cached bot names
  (`BOT_NAMES`). Token posts are recorded in `RECENT_OUTBOUND`/`message_links` so they
  never echo back.
- **Text rendering**: Discord → GroupMe starts every message with `Nickname:` (including
  picture-only posts), turns user/role mentions into `@Name` and channels into `#name`, and
  strips emoji. Do not reintroduce Discord markup. GroupMe → Discord relays the text
  untouched — no `#topic:` prefix or channel label, because each destination mirrors one
  conversation — except that `mentions` attachments are never printed as `[mentions]`:
  their `loci` are re-rendered as bold `\@name`, with the `@` backslash-escaped so a
  Discord member of the same name is never pinged. GroupMe replies become blockquotes
  (`groupme_reply_quote`); other unknown attachment types still render as `[type]`.
- **Commands are administrator-only twice over**: `bridge_group`'s
  `default_permissions=administrator` plus `require_manager`. Keep both when adding
  commands.
- **`sync_commands()`** publishes guild-scoped commands when `DISCORD_GUILD_ID` is set and
  calls `purge_global_commands()` first. A global set from a run without a guild id
  survives every guild resync, shows up twice, and breaks autocomplete with
  `CommandSignatureMismatch`. Keep that purge.

## Images

`compress_image` (Pillow) downscales to `IMAGE_MAX_DIMENSION`, re-encodes as JPEG at
`IMAGE_JPEG_QUALITY`, and lowers quality until under `IMAGE_UPLOAD_LIMIT_BYTES`, then
uploads to `image.groupme.com`. Animated GIFs pass through untouched. GroupMe rejects
non-`i.groupme.com` URLs (`invalid origin`), so never post a raw Discord CDN URL as
`picture_url`; without an access token images are skipped and the message says how many
were dropped. Use `read_body_limited` for downloads — `StreamReader.read(n)` truncates.

## Config and ops

- Configuration comes from environment variables. Never commit credentials; keep
  `.env.example` in step with new settings. `.env` is read by `python-dotenv`, not by
  systemd.
- Webhook mode can expose the aiohttp callback server through the `ngrok` SDK
  (`NGROK_ENABLED`, `NGROK_DOMAIN`, `NGROK_AUTHTOKEN`). The tunnel URL replaces
  `PUBLIC_BASE_URL` at startup and must match what `/bridge add` prints.
  `GROUPME_WEBHOOK_HOST` defaults to `127.0.0.1`: GroupMe does not sign callbacks, so the
  endpoint must not be reachable from the internet. Only set `0.0.0.0` for a proxy on
  another host. `GROUPME_WEBHOOK_PATH` is the only secret protecting it.
- `requirements.txt` pins ngrok by interpreter (`>=1.4,<1.5` below 3.12, `>=1.7` above);
  both SDKs support the calls used in `start_tunnel`. Check `requires_python` on PyPI
  before raising the floor. Avoid new dependencies for what the stdlib already does.
- `deploy/catbird-bot.service` runs `bot.py` from `/opt/catbirdBot` as `catbird`; keep
  `WorkingDirectory` in sync with the README and keep `deploy/` tracked by git.

## Working on it

- Edits go in `bot.py`, reusable scripts in `utils/`, checks in `tests/`.
- After Python changes run `python -m py_compile bot.py utils\*.py tests\*.py`, then
  `python tests\smoke.py` and `python tests\integration.py` (the last one runs a fake
  GroupMe API on port 8731 with a temporary database).
