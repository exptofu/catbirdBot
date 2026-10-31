# Catbird Bot

A two-way bridge between GroupMe groups and Discord channels. Any number of GroupMe groups can be paired with any number of channels in a single Discord server, and the pairings are managed with a `/bridge` slash command.

## Features

- **Many-to-many bridges** in one Discord server, stored in SQLite (`catbird.db`).
- **Topic bridges**: a GroupMe group with several topics can relay its main chat *and* one chosen topic into a single channel; Discord messages are posted into that topic.
- **Both directions**: group bridges post with a GroupMe bot; topic bridges post with the access token; GroupMe messages are posted through a per-channel Discord webhook so the original name and avatar are preserved.
- **Photos**: GroupMe images are downloaded and re-uploaded as Discord attachments; Discord images are re-hosted through GroupMe's image service (topic bridges) or handed to the group bot as `picture_url`.
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
2. Create one GroupMe bot per group you want to bridge, with the group's id as `bot[group_id]`:

   ```powershell
   python utils/groupme_setup.py groups
   python utils/groupme_setup.py create-bot --group-id 12345678 --callback-url https://example.com/groupme/callback
   ```

   The command prints the `bot_id` to use with `/bridge add`.
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
| `/bridge add group_id channel [bot_id] [topic]` | Create a bridge, create the channel webhook, and baseline the conversation so old history is not replayed. Accepts a group id or a `groupme.com` join link. `bot_id` is required for a group bridge; `topic` (a topic name or id) creates a topic bridge instead. Re-running it for an already bridged group updates that bridge. |
| `/bridge topics group_id` | List the group's topics with their ids and message counts. |
| `/bridge set-topic group_id topic` | Point an existing bridge at a topic, or pass `none` to go back to the main chat. |
| `/bridge list` | Show every group/topic, channel, and poster pairing. |
| `/bridge remove group_id` | Delete the bridge, its webhook, and its message links. |
| `/bridge set-bot group_id bot_id` | Point an existing group bridge at a different GroupMe bot. |
| `/bridge test group_id` | Post one test message in each direction. |

Slash commands sync globally on startup. Set `DISCORD_GUILD_ID` to sync to a single guild instead, which is instant.

## GroupMe topics

A GroupMe **topic** is a subgroup with its own message stream, its own id, and no bot support. A topic bridge is therefore asymmetric:

- **GroupMe → Discord**: the group's main chat (via bot callbacks) and the mapped topic (via polling) both relay into the same channel. Other topics are ignored.
- **Discord → GroupMe**: messages are posted into the mapped topic with `GROUPME_ACCESS_TOKEN`, so they appear as the authenticated GroupMe account rather than a bot. Images are uploaded to `image.groupme.com` first so they render inline.

`POST /groups/{id}/messages` is the only way to write into a topic; creating a bot for a subgroup returns `401`, and topic messages never trigger bot callbacks, which is why they are polled.

A GroupMe group without topics keeps the plain 1:1 mapping: `/bridge add group_id channel bot_id` with no `topic`.


## Edits

Not mirrored. GroupMe's public API exposes only *index* and *create* for messages — no edit or delete endpoint — so a Discord edit cannot be applied to the GroupMe copy, and GroupMe never reports edited content.

## Behaviour notes

- GroupMe text is capped at 1,000 characters. Longer Discord messages are truncated and, when possible, keep their trailing links.
- Discord replies are relayed with a quoted snippet. User and role mentions become `@Name` and channel mentions become `#name`, because GroupMe does not render Discord markup. Emoji are stripped: custom Discord emoji have no GroupMe equivalent and unicode emoji are not relayed.
- GroupMe messages are mirrored with `AllowedMentions.none()`, so `@everyone` in GroupMe cannot ping Discord.
- Bot filtering: Discord messages with `author.bot` or a `webhook_id` are skipped (the bridge's own webhooks included). GroupMe messages with `system: true`, `sender_type` other than `user`, no `user_id`, or a name matching a bot owned by the access token are skipped, so give each bridge bot a distinctive name.
- Topic posts are made with the access token, so they come back from the API as ordinary user messages; the bridge stores their ids and never mirrors them back into Discord.
- The poller makes one request per polled conversation per cycle (topics always, groups only in `poll` mode). GroupMe rate limits per token, so raise `GROUPME_POLL_SECONDS` when bridging many conversations.
- The first poll for a conversation baselines at its newest message; history is never replayed.
- Images larger than 8 MiB are skipped rather than uploaded.

## Files

| Path | Purpose |
| --- | --- |
| `bot.py` | The bridge: Discord client, GroupMe API client, relays, and slash commands. |
| `utils/groupme_setup.py` | Lists groups/bots and creates or updates the GroupMe bot. |
| `catbird.db` | SQLite store for bridges, poll cursors, and message links. Safe to delete only when every bridge can be recreated. |
