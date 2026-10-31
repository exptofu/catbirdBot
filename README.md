# Catbird Bot

A two-way bridge between GroupMe groups and Discord channels. Any number of GroupMe groups can be paired with any number of channels in a single Discord server, and the pairings are managed with a `/bridge` slash command.

## Features

- **Many-to-many bridges** in one Discord server, stored in SQLite (`catbird.db`).
- **Topic bridges**: a GroupMe group with several topics can relay its main chat *and* one chosen topic into a single channel; Discord messages are posted into that topic.
- **Both directions**: group bridges post with a GroupMe bot; topic bridges post with the access token; GroupMe messages are posted through a per-channel Discord webhook so the original name and avatar are preserved.
- **Photos**: GroupMe images are downloaded and re-uploaded as Discord attachments; Discord images are downloaded, compressed, and re-hosted on GroupMe's image service for both group and topic bridges. GroupMe rejects non-GroupMe image URLs (`invalid origin`), so an image that cannot be re-hosted is skipped and the message notes how many were dropped.
- **No bot echo**: bot messages are skipped on both sides, and topic posts are tracked so they are not mirrored back.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `.env`, then run:

```powershell
python bot.py
```

`bot.py` needs the **Message Content** privileged intent enabled for the application in the Discord Developer Portal, plus `View Channel`, `Send Messages`, `Read Message History`, and `Manage Webhooks` in the bridged channels. The bot logs its invite URL on startup.

## GroupMe setup

1. Copy the access token from <https://dev.groupme.com/applications> into `GROUPME_ACCESS_TOKEN`.
2. Optional: pre-create bots. `/bridge add` creates one automatically for each new group (name `GROUPME_BOT_NAME`, default "Catbird Bridge"), reusing the existing bot when the callback URL is already registered. To manage bots manually:

   ```powershell
   python utils/groupme_setup.py groups
   python utils/groupme_setup.py create-bot --group-id 12345678 --callback-url https://example.com/groupme/callback
   ```

   The command prints the `bot_id`, which can be passed to `/bridge add` instead of letting it create one.
3. Choose a transport:

   | `GROUPME_MODE` | How GroupMe messages arrive | Requirements |
   | --- | --- | --- |
   | `poll` (default) | The bot reads group history every `GROUPME_POLL_SECONDS` using the access token | No public URL |
   | `webhook` | GroupMe POSTs each group message to the bot's callback URL | A public URL, forwarded to `GROUPME_WEBHOOK_PORT` + `GROUPME_WEBHOOK_PATH` |

   In `webhook` mode every bridge bot's callback URL must point at the same endpoint. Use a long, unguessable `GROUPME_WEBHOOK_PATH`; GroupMe does not sign callbacks, so the path is the only secret protecting the endpoint.

   ### ngrok tunnel

   Set `NGROK_ENABLED=true` to have the bot run its own tunnel, so no separate ngrok process or reverse proxy is needed:

   ```dotenv
   GROUPME_MODE=webhook
   GROUPME_WEBHOOK_PORT=8085
   NGROK_ENABLED=true
   NGROK_AUTHTOKEN=<your ngrok token>          # read by the ngrok SDK
   NGROK_DOMAIN=paddling-unmoved-equator.ngrok-free.dev
   ```

   The tunnel URL is logged on startup and used by `/bridge add` to print the callback URL to paste into the GroupMe bot settings. Leave `NGROK_DOMAIN` empty to get a new random URL each run, and set `GROUPME_PUBLIC_URL` instead if you front the port with your own proxy.


## Slash commands

All commands require the **Administrator** permission (enforced both by Discord's command permissions and by the bot) and reply ephemerally.

| Command | Purpose |
| --- | --- |
| `/bridge add group_id channel [bot_id] [topic]` | Create a bridge, create the channel webhook, and baseline the conversation so old history is not replayed. Accepts a group id or a `groupme.com` join link. With `GROUPME_ACCESS_TOKEN` set, `bot_id` is optional: the bot is created for that group automatically (or the existing one is reused when the callback URL is already registered). `topic` (a topic name or id) creates a topic bridge instead. Re-running it for an already bridged group updates that bridge. |
| `/bridge topics group_id` | List the group's topics with their ids and message counts. |
| `/bridge set-topic group_id topic` | Point an existing bridge at a topic, or pass `none` to go back to the main chat. |
| `/bridge list` | Show every group/topic, channel, and poster pairing. |
| `/bridge remove group_id` | Delete the bridge, its webhook, and its message links. |
| `/bridge set-bot group_id bot_id` | Point an existing group bridge at a different GroupMe bot. |
| `/bridge set-avatar group_id [image_url]` | Give the group's bot a profile picture. GroupMe has no bot update endpoint, so the bot is deleted and recreated with the avatar (new bot id). Defaults to the Discord bot's own avatar. |
| `/bridge test group_id` | Post one test message in each direction. |

Slash commands sync globally on startup. Set `DISCORD_GUILD_ID` to sync to a single guild instead, which is instant.

## GroupMe topics

A GroupMe **topic** is a subgroup with its own message stream, its own id, and no bot support. A topic bridge is therefore asymmetric:

- **GroupMe → Discord**: the group's main chat (via bot callbacks) and the mapped topic (via polling) both relay into the same channel. Other topics are ignored.
- **Discord → GroupMe**: messages are posted into the mapped topic with `GROUPME_ACCESS_TOKEN`, so they appear as the authenticated GroupMe account rather than a bot. Images are uploaded to `image.groupme.com` first so they render inline.

`POST /groups/{id}/messages` is the only way to write into a topic; creating a bot for a subgroup returns `401`, and topic messages never trigger bot callbacks, which is why they are polled.

A topic bridge still gets a bot on the **parent** group — it never posts with it, but that bot is what makes the group's main chat arrive by callback. If no bot can be created (or one was removed later), the bot falls back to polling the parent group, so main-chat messages still arrive, just on the `GROUPME_POLL_SECONDS` cadence.

### Bot avatars

GroupMe bots support a `bot[avatar_url]`, but **only** when it points at `i.groupme.com`; any other origin is silently dropped (`avatar_url: null`). Point `GROUPME_BOT_AVATAR_PATH` at an image in the repo (e.g. `avatar.png`) and the bot uploads a fresh copy for every bot it creates; if that is empty it falls back to `GROUPME_BOT_AVATAR_URL`, then to the Discord bot's own picture.

Avatars are never shared between bots or reused across restarts: GroupMe deletes the hosted image when a bot that used it is removed, so a pinned URL silently starts returning `403 AccessDenied`. GroupMe also exposes no bot update call, so changing an avatar recreates the bot — `/bridge set-avatar` does that and stores the new bot id.

A GroupMe group without topics keeps the plain 1:1 mapping: `/bridge add group_id channel bot_id` with no `topic`.


## Edits

Not mirrored. GroupMe's public API exposes only *index* and *create* for messages — no edit or delete endpoint — so a Discord edit cannot be applied to the GroupMe copy, and GroupMe never reports edited content.

## Behaviour notes

- GroupMe text is capped at 1,000 characters. Longer Discord messages are truncated and, when possible, keep their trailing links.
- Discord replies are relayed with a quoted snippet. Every Discord message starts with `Nickname:` (including picture-only posts, which are separate bot messages), so GroupMe members can see who typed. User and role mentions become `@Name` and channel mentions become `#name`, because GroupMe does not render Discord markup. Emoji are stripped: custom Discord emoji have no GroupMe equivalent and unicode emoji are not relayed.
- GroupMe messages keep the sender's name and avatar through the channel webhook. Messages relayed from a topic are prefixed with `#topic:` (e.g. `#cobb: hey`) so a single channel can carry the main chat and the mapped topic; main-chat messages are unprefixed. The Discord → GroupMe direction is delineated the same way, with `Nickname:` leading every message.
- GroupMe messages are mirrored with `AllowedMentions.none()`, so `@everyone` in GroupMe cannot ping Discord.
- Bot filtering: Discord messages with `author.bot` or a `webhook_id` are skipped (the bridge's own webhooks included). GroupMe messages with `system: true`, `sender_type` other than `user`, no `user_id`, or a name matching a bot owned by the access token are skipped, so give each bridge bot a distinctive name.
- Topic posts are made with the access token, so they come back from the API as ordinary user messages; the bridge stores their ids and never mirrors them back into Discord.
- GroupMe answers `404` on `/bots/post` when a bot id no longer exists (deleted, recreated, or copied from another account). The bridge treats that as a stale mapping: it looks up the group's bot (creating one with the configured name, avatar, and callback if needed), saves the new id, and retries the post once.
- The poller makes one request per polled conversation per cycle (topics always, groups only in `poll` mode). GroupMe rate limits per token, so raise `GROUPME_POLL_SECONDS` when bridging many conversations.
- The first poll for a conversation baselines at its newest message; history is never replayed.
- Every image sent to GroupMe is compressed: downscaled to `IMAGE_MAX_DIMENSION` (default 2048 px) and re-encoded as JPEG at `IMAGE_JPEG_QUALITY` (default 85), lowering quality until it fits `IMAGE_UPLOAD_LIMIT_BYTES` (default 8 MiB), then re-hosted on `image.groupme.com`. Animated GIFs are kept untouched (re-encoding would drop frames) and are skipped if they exceed the budget. Originals above `IMAGE_SOURCE_LIMIT_BYTES` (32 MiB) are dropped.
- GroupMe validates the `picture_url` origin: a Discord CDN link or any other non-`i.groupme.com` host returns `400 ... has invalid origin`. That is why images are always downloaded and re-hosted rather than passed through, and why `GROUPME_ACCESS_TOKEN` is effectively required for image relay.

## Files

| Path | Purpose |
| --- | --- |
| `bot.py` | The bridge: Discord client, GroupMe API client, relays, and slash commands. |
| `utils/groupme_setup.py` | Lists GroupMe groups/bots, creates or updates a bot, sets callbacks. |
| `utils/bridge_setup.py` | Creates a bridge without slash commands (webhook, bot, topic, baselines). |
| `utils/bot_admin.py` | Bot maintenance: `list` (with avatar reachability), `ensure`, `repair`, `resync-avatars`. |
| `utils/sync_env.py` | Rebuilds `.env` from `.env.example` while preserving existing values. |
| `utils/inspect_payloads.py` | Summarizes captured GroupMe callbacks (`groupme_payloads.jsonl`). |
| `tests/smoke.py` | Fast checks for DB helpers, rendering, and topic matching. |
| `tests/integration.py` | Offline end-to-end run against a fake GroupMe API (relays, images, bot recovery). |
| `catbird.db` | SQLite store for bridges, poll cursors, and message links. Safe to delete only when every bridge can be recreated. |

Run the checks with:

```powershell
python -m py_compile bot.py utils\*.py tests\*.py
python tests\smoke.py
python tests\integration.py
```

The integration test starts its own fake GroupMe API on `127.0.0.1:8731` and uses a temporary database, so it never touches the live bridge.
