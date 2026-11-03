# Catbird Bot

A two-way bridge between GroupMe and Discord. Each GroupMe conversation — a group's main chat or one of its topics — is paired with one Discord destination, either a text channel or a forum post, in a single Discord server. Pairings are managed with the `/bridge` slash command.

## Features

- **One-to-one bridges**: every GroupMe conversation — a group's main chat, or one of its topics — maps to exactly one Discord destination, either a text channel or a forum post. Bridges live in SQLite (`catbird.db`).
- **Destination-aware commands**: `/bridge add` looks at where you ran it. In a text channel the channel is the destination, in a forum channel the bot creates a post, and inside a post or thread that post is the destination.
- **Forum posts** behave like any other destination: GroupMe messages arrive through the webhook that lives on the forum channel, and an archived post is unarchived automatically before the next message.
- **Both directions**: main-chat bridges post with a GroupMe bot; topic bridges post with the access token; GroupMe messages are posted through a per-channel Discord webhook so the original name and avatar are preserved.
- **No bot required**: when a group refuses bot creation (GroupMe answers `401` for that group, even in its own UI), the bridge falls back to posting with `GROUPME_ACCESS_TOKEN` and polls the group every `GROUPME_POLL_SECONDS` instead of relying on callbacks.
- **Photos**: GroupMe images are downloaded and re-uploaded as Discord attachments; Discord images are downloaded, compressed, and re-hosted on GroupMe's image service for both group and topic bridges. GroupMe rejects non-GroupMe image URLs (`invalid origin`), so an image that cannot be re-hosted is skipped and the message notes how many were dropped.
- **No bot echo**: bot messages are skipped on both sides, and token posts are tracked so they are not mirrored back.
- **Deletion sync**: deleting a bridged Discord message deletes the GroupMe message(s) it produced; deleting a GroupMe message removes its Discord copy. Set `SYNC_DELETIONS=false` to turn this off.

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

`bot.py` needs the **Message Content** privileged intent enabled for the application in the Discord Developer Portal, plus `View Channel`, `Send Messages`, `Read Message History`, and `Manage Webhooks` in the bridged channels. `Manage Messages` is recommended: it is the fallback that deletes a mirrored Discord message when the bridge webhook has been replaced. The bot logs its invite URL on startup.

## Deploy (systemd)

`deploy/catbird-bot.service` runs the bot from `/opt/catbirdBot` as an unprivileged `catbird` user:

```bash
sudo useradd --system --home /opt/catbirdBot --shell /usr/sbin/nologin catbird
sudo mkdir -p /opt/catbirdBot
sudo rsync -a --exclude .venv --exclude .git ./ /opt/catbirdBot/
cd /opt/catbirdBot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp deploy/catbird-bot.service /etc/systemd/system/
sudo chown -R catbird:catbird /opt/catbirdBot
sudo chmod 600 /opt/catbirdBot/.env
sudo systemctl daemon-reload
sudo systemctl enable --now catbird-bot
```

Create `.env` in `/opt/catbirdBot` before the first start (it is read by `python-dotenv`, not by systemd). `catbird.db` and the avatar file live in the same directory, so the service user needs write access there.

```bash
sudo systemctl status catbird-bot
sudo journalctl -u catbird-bot -f      # logs (PYTHONUNBUFFERED=1, so no buffering)
sudo systemctl restart catbird-bot
```

The unit uses `KillSignal=SIGINT` because `discord.py` shuts down on SIGINT through `Bot.close()`, which closes the ngrok tunnel and the aiohttp callback server before exit. `Restart=on-failure` with `RestartSec=10` covers crashes and transient network failures.

## GroupMe setup

1. Copy the access token from <https://dev.groupme.com/applications> into `GROUPME_ACCESS_TOKEN`.
2. Optional: pre-create bots. `/bridge add` creates one automatically for each new group (name `GROUPME_BOT_NAME`, default "Catbird Bridge"), reusing the existing bot when the callback URL is already registered. To manage bots manually:

   ```powershell
   python utils/groupme_setup.py groups
   python utils/groupme_setup.py create-bot --group-id 12345678 --callback-url https://example.com/groupme/callback
   ```

  The command prints the `bot_id`, which can be passed to `/bridge add` instead of letting it create one. Some groups do not allow bots at all (`POST /bots` returns `401` for them, even in GroupMe's own UI); such groups are still bridgeable, because the bridge falls back to posting with the access token.
3. Choose a transport:

   | `GROUPME_MODE` | How GroupMe messages arrive | Requirements |
   | --- | --- | --- |
   | `poll` (default) | The bot reads group history every `GROUPME_POLL_SECONDS` using the access token | No public URL |
   | `webhook` | GroupMe POSTs each group message to the bot's callback URL | A public URL, forwarded to `GROUPME_WEBHOOK_PORT` + `GROUPME_WEBHOOK_PATH` |

   In `webhook` mode every bridge bot's callback URL must point at the same endpoint. Use a long, unguessable `GROUPME_WEBHOOK_PATH`; GroupMe does not sign callbacks, so the path is the only secret protecting the endpoint. `GROUPME_WEBHOOK_HOST` defaults to `127.0.0.1`, so the port stays off the internet when ngrok or a proxy on the same host forwards to it; set it to `0.0.0.0` only if the proxy runs on another machine.

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

   `requirements.txt` pins ngrok by Python version: 1.5+ requires Python ≥ 3.12, so Python 3.10/3.11 gets `ngrok 1.4.x` (verified against the same `forward`/`domain`/`close` calls). If ngrok cannot be installed at all (restricted index, no egress), set `NGROK_ENABLED=false` and expose the port yourself with `GROUPME_PUBLIC_URL`.


## Slash commands

All commands require the **Administrator** permission (enforced both by Discord's command permissions and by the bot) and reply ephemerally.

| Command | Purpose |
| --- | --- |
| `/bridge add group_id [topic] [bot_id]` | Bridge the conversation into wherever the command was run: a text channel, a forum channel (a post is created), or an existing post/thread. Accepts a group id or a `groupme.com` join link. `topic` (name or id) mirrors that topic instead of the main chat. `bot_id` is optional when `GROUPME_ACCESS_TOKEN` is set: a bot is created or reused for main-chat bridges, and if the group refuses bots the bridge falls back to posting with the access token. The cursor is baselined so old history is not replayed. |
| `/bridge topics group_id` | List the group's topics with their ids and message counts. |
| `/bridge set-topic topic` | Point the bridge on this channel/post at another GroupMe topic, or pass `none` to mirror the group's main chat instead. |
| `/bridge list` | Show every bridge: GroupMe conversation, Discord destination, and poster. |
| `/bridge remove` | Delete the bridge on this channel/post, its webhook, and its message links. |
| `/bridge set-bot bot_id` | Point a main-chat bridge at a different GroupMe bot. |
| `/bridge set-mode user\|bot` | Choose the posting path for a main-chat bridge. `user` posts with `GROUPME_ACCESS_TOKEN`, so GroupMe returns the message id and Discord deletions remove the copy; `bot` posts through the GroupMe bot. The bot stays attached either way for inbound callbacks, so switching to `user` does not add polling latency. |
| `/bridge set-avatar [image_url]` | Give the bridge's bot a profile picture. GroupMe has no bot update endpoint, so the bot is deleted and recreated with the avatar (new bot id). Defaults to the Discord bot's own avatar. |
| `/bridge test` | Post one test message in each direction for the bridge on this channel/post. |

Only `/bridge add` (which takes a `group_id`) and `/bridge topics` take a GroupMe id: every other command acts on the channel or post it is run in.

Slash commands sync on every startup. Set `DISCORD_GUILD_ID` to sync to a single guild instead, which is instant.

With `DISCORD_GUILD_ID` set, the bot also deletes any **globally** registered commands left over from an earlier run that had no guild id. A stale global copy survives every later guild resync, appears next to the guild copy in Discord's picker, and raises `CommandSignatureMismatch` in autocomplete once the code's parameters no longer match it. If a command still shows old parameters after a restart, press `Ctrl+R` in Discord so the client re-fetches the command list.

## GroupMe topics

A GroupMe **topic** is a subgroup with its own message stream, its own id, and no bot support. Topics are bridged one-to-one, independently of the group's main chat:

- **GroupMe → Discord**: only the mapped conversation relays, never both. A topic bridge polls its own topic; a main-chat bridge gets bot callbacks, or polls when `GROUPME_MODE=poll` or when the bridge has no bot.
- **Discord → GroupMe**: a main-chat bridge posts with the group's bot (or with the access token if the group forbids bots, or after `/bridge set-mode user`); a topic bridge always posts with `GROUPME_ACCESS_TOKEN`, so it appears as the authenticated GroupMe account. Images are uploaded to `image.groupme.com` first so they render inline.

One group can therefore have two bridges at the same time — the main chat in one channel and `#cobb` in a forum post — but each conversation has exactly one Discord destination and each destination carries exactly one bridge. `bridges.conversation_id` is the primary key, `bridges.channel_id` is unique, and `/bridge add` refuses a conversation or a destination that is already taken.

`POST /groups/{id}/messages` is the only way to write into a topic; creating a bot for a subgroup returns `401`, and topic messages never trigger bot callbacks, which is why they are polled. A topic bridge needs no GroupMe bot at all.

### Bot avatars

GroupMe bots support a `bot[avatar_url]`, but **only** when it points at `i.groupme.com`; any other origin is silently dropped (`avatar_url: null`). Point `GROUPME_BOT_AVATAR_PATH` at an image in the repo (e.g. `avatar.png`) and the bot uploads a fresh copy for every bot it creates; if that is empty it falls back to `GROUPME_BOT_AVATAR_URL`, then to the Discord bot's own picture.

Avatars are never shared between bots or reused across restarts: GroupMe deletes the hosted image when a bot that used it is removed, so a pinned URL silently starts returning `403 AccessDenied`. GroupMe also exposes no bot update call, so changing an avatar recreates the bot — `/bridge set-avatar` does that and stores the new bot id.


## Migrating to a new GroupMe account

Only two things are tied to a GroupMe account:

| Thing | Where | Consequence |
| --- | --- | --- |
| `GROUPME_ACCESS_TOKEN` | `.env` (and `/opt/catbirdBot/.env` when deployed) | Polling, group and topic lookups, image uploads, and topic posts all run as this account |
| GroupMe bots (`bridges.bot_id`) | created by `POST /bots` | `GET /bots` only lists bots owned by the token; a bot id from another account makes `/bots/post` answer `404` |

`catbird.db` holds no account or user ids, so a new token needs no export/import and the poll cursors keep working (history is not replayed). Note that bumping `SCHEMA_VERSION` rebuilds the bridge tables, so existing bridges then have to be re-created with `/bridge add`.

1. **Create the account and join every bridged group.** GroupMe allows one account per phone number, so use a dedicated email and phone. `/groups`, `/groups/{id}/subgroups`, bot creation, and topic posts all fail for groups the account is not a member of.
2. **Copy the new token** while logged in as the new account: <https://dev.groupme.com/applications> → Access Token.
3. **Delete the old bots, still using the old token.** Bots cannot change owner, and GroupMe rejects a new bot whose callback URL is *"already registered for the group"*. `ensure_group_bot` then looks the bot up through `GET /bots`, which only lists the new account's bots, so the bridge would be left without one — it now falls back to posting with the access token, but the bridged group loses bot attribution until a bot is recreated.

   ```powershell
   python utils/bot_admin.py list
   python utils/groupme_setup.py delete-bot --bot-id <bot_id>   # one per bridged group
   ```

   If the old token is already gone, leave `GROUPME_PUBLIC_URL` and `NGROK_DOMAIN` empty in `poll` mode instead: a bot without a callback URL does not collide with the old one.
4. **Swap `GROUPME_ACCESS_TOKEN`** in the local `.env` and in `/opt/catbirdBot/.env` (`sudo chmod 600`), then restart the service. Never commit the token.
5. **Check membership before recreating bots:** `python utils/groupme_setup.py groups` must list every group from `/bridge list`, or the poller logs `GroupMe poll failed` for the missing ones.
6. **Recreate the bots** — this creates them under the new account and stores the new ids in the bridge, reusing `GROUPME_BOT_NAME` and the configured avatar and callback:

   ```powershell
   python utils/bot_admin.py ensure --group <group_id>   # per bridged group
   ```

   Re-running `/bridge add group_id:<id>` in the channel also updates the bridge, but it creates a fresh Discord webhook each time, so `bot_admin.py ensure` is the tidier route.
7. **Restart and verify.** `refresh_bot_names()` rebuilds `BOT_NAMES` from the new token at startup. Run `/bridge test` in each bridged channel/post and watch `journalctl -u catbird-bot -f` for `404`/`403` responses.

Visible effects: topic bridges (`send_mode='user'`) now post as the new account, including its name and avatar; group bridges still post through the bot, so only the bot's owning account changes. Keep the new account in the groups permanently — removing it breaks polling, topic relay, and image upload even though the bots keep firing callbacks. The old account can leave the groups after the new bots post successfully, but keep it alive until then, because its token is the only way to delete its bots.


## Edits and deletions

**Edits** are not mirrored. GroupMe's public API exposes only *index* and *create* for messages, so a Discord edit cannot be applied to the GroupMe copy, and GroupMe never reports edited content.

**Deletions** are mirrored both ways when `SYNC_DELETIONS=true` (the default):

- **Discord → GroupMe**: deleting a bridged Discord message (or purging it) deletes the GroupMe message(s) the bridge created for it. The bridge stores the GroupMe id of every post it makes: token posts return it directly, and bot posts are matched afterwards by text and picture URL because `POST /bots/post` returns no message. GroupMe's public docs list only index/create, but its own clients delete through `DELETE /conversations/:conversation_id/messages/:message_id`, which is what the bridge uses. If GroupMe refuses (for example a bot post the token owner may not remove), the Discord deletion still happens and the failure is logged.
- **GroupMe → Discord**: GroupMe sends no delete callback, so every `GROUPME_DELETE_CHECK_SECONDS` (default 300) the newest 100 messages of each bridged conversation are compared with the stored links. A linked message that is missing from that window has its Discord copy deleted. Messages older than the window are left alone rather than paging through the entire history.

Deleting a *mirrored* GroupMe message in Discord only removes the copy; the GroupMe original stays, because the bridge must not let a Discord cleanup wipe GroupMe history.

Bot posts are owned by the GroupMe bot, and some groups refuse to let the token's account delete them (`403 User not allowed to delete (group deletion mode restriction)`). `/bridge set-mode user` switches a main-chat bridge to posting with the access token; the bot stays attached for callbacks, so inbound remains instant and deletions become exact.

`SYNC_DELETIONS` needs `GROUPME_ACCESS_TOKEN` for the GroupMe side; without it, Discord deletions are logged and skipped.

## Behaviour notes

- GroupMe text is capped at 1,000 characters. Longer Discord messages are truncated and, when possible, keep their trailing links.
- Discord replies are relayed with a quoted snippet. Every Discord message starts with `Nickname:` (including picture-only posts, which are separate bot messages), so GroupMe members can see who typed. User and role mentions become `@Name` and channel mentions become `#name`, because GroupMe does not render Discord markup. Emoji are stripped: custom Discord emoji have no GroupMe equivalent and unicode emoji are not relayed.
- GroupMe messages keep the sender's name and avatar through the channel webhook, and their text is relayed exactly as sent: since every destination mirrors a single conversation, no channel or topic prefix is added. The Discord → GroupMe direction is delineated with `Nickname:` leading every message.
- GroupMe replies are relayed as a Markdown quote above the text, `> Ann: meet at 7?`, mirroring how Discord replies reach GroupMe. When GroupMe omits the quoted text the line becomes `> Replying to Ann`.
- Forum posts and threads have no webhook of their own: the bridge creates the webhook on the parent forum/text channel and passes the thread id when posting. An archived post is unarchived before the message is sent.
- GroupMe messages are mirrored with `AllowedMentions.none()`, so `@everyone` in GroupMe cannot ping Discord.
- Bot filtering: Discord messages with `author.bot` or a `webhook_id` are skipped (the bridge's own webhooks included). GroupMe messages with `system: true`, `sender_type` other than `user`, no `user_id`, or a name matching a bot owned by the access token are skipped, so give each bridge bot a distinctive name.
- Posts made with the access token (topic bridges, and groups that refuse bots) come back from the API as ordinary user messages; the bridge stores their ids and never mirrors them back into Discord.
- GroupMe answers `404` on `/bots/post` when a bot id no longer exists (deleted, recreated, or copied from another account). The bridge treats that as a stale mapping: it looks up the group's bot (creating one with the configured name, avatar, and callback if needed), saves the new id, and retries the post once.
- The poller makes one request per polled conversation per cycle (topics always, main chats only in `poll` mode or when the bridge has no bot). GroupMe rate limits per token, so raise `GROUPME_POLL_SECONDS` when bridging many conversations.
- The first poll for a conversation baselines at its newest message; history is never replayed.
- Every image sent to GroupMe is compressed: downscaled to `IMAGE_MAX_DIMENSION` (default 2048 px) and re-encoded as JPEG at `IMAGE_JPEG_QUALITY` (default 85), lowering quality until it fits `IMAGE_UPLOAD_LIMIT_BYTES` (default 8 MiB), then re-hosted on `image.groupme.com`. Animated GIFs are kept untouched (re-encoding would drop frames) and are skipped if they exceed the budget. Originals above `IMAGE_SOURCE_LIMIT_BYTES` (32 MiB) are dropped.
- `/bridge add` creates a GroupMe bot for main-chat bridges. When GroupMe refuses the request (a refused `avatar_url`, a per-group bot limit, a callback URL already registered for the group), the bot is retried without its avatar and the bridge falls back to any bot the account already has for that group; either way the reply quotes GroupMe's own error instead of a generic message.
- Some groups do not allow bots at all: `POST /bots` answers `401 unauthorized` for them even from GroupMe's own UI. Rather than failing, the bridge then posts with `GROUPME_ACCESS_TOKEN` — Discord messages appear as the authenticated GroupMe account rather than a bot (the `Nickname:` prefix keeps attribution) — and the group is polled for GroupMe transcripts because a bot is what would have delivered callbacks.
- GroupMe validates the `picture_url` origin: a Discord CDN link or any other non-`i.groupme.com` host returns `400 ... has invalid origin`. That is why images are always downloaded and re-hosted rather than passed through, and why `GROUPME_ACCESS_TOKEN` is effectively required for image relay.

## Files

| Path | Purpose |
| --- | --- |
| `bot.py` | The bridge: Discord client, GroupMe API client, relays, and slash commands. |
| `utils/groupme_setup.py` | Lists GroupMe groups/bots, creates or updates a bot, sets callbacks. |
| `utils/bridge_setup.py` | Creates a bridge without slash commands (`--channel`, or `--thread <post> --parent <forum>`); bot, topic, and baselines handled. |
| `utils/bot_admin.py` | Bot maintenance: `list` (with avatar reachability), `ensure`, `repair`, `resync-avatars`. |
| `utils/sync_env.py` | Rebuilds `.env` from `.env.example` while preserving existing values. |
| `utils/inspect_payloads.py` | Summarizes captured GroupMe callbacks (`groupme_payloads.jsonl`). |
| `utils/check_commands.py` | Compares the slash commands registered in Discord with the ones in `bot.py`; `--fix` deletes a stale global command set. |
| `tests/smoke.py` | Fast checks for DB helpers and bridge constraints, rendering, and topic matching. |
| `tests/integration.py` | Offline end-to-end run against a fake GroupMe API (relays, images, bot creation fallbacks, token posting). |
| `deploy/catbird-bot.service` | systemd unit that runs the bot from `/opt/catbirdBot` as the `catbird` user. |
| `catbird.db` | SQLite store for bridges, poll cursors, message links, and outbound GroupMe ids used by deletion sync. Safe to delete only when every bridge can be recreated. |

Run the checks with:

```powershell
python -m py_compile bot.py utils\*.py tests\*.py
python tests\smoke.py
python tests\integration.py
```

The integration test starts its own fake GroupMe API on `127.0.0.1:8731` and uses a temporary database, so it never touches the live bridge.
