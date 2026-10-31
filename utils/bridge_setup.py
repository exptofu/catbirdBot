"""Create or update a bridge without slash commands.

Creates the Discord webhook, ensures the GroupMe bot, resolves the topic if given,
and baselines the conversation so old history is not replayed. Mirrors `/bridge add`.

    python utils/bridge_setup.py --group 118478056 --channel 1558263681114968215
    python utils/bridge_setup.py --group 118478056 --channel 1558263681114968215 --topic cobb
    python utils/bridge_setup.py --group 118478056 --thread 1565911029269930024 --parent 1565911029269930020
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import bot  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create a GroupMe <-> Discord bridge")
    parser.add_argument("--group", required=True, help="GroupMe group id")
    parser.add_argument(
        "--channel", type=int, default=0, help="Discord text channel id (destination)"
    )
    parser.add_argument(
        "--thread",
        type=int,
        default=0,
        help="Discord forum post / thread id (destination); implies a thread target",
    )
    parser.add_argument(
        "--parent",
        type=int,
        default=0,
        help="Forum or text channel that owns the webhook (required with --thread)",
    )
    parser.add_argument("--topic", default="", help="Topic name or id (optional)")
    parser.add_argument("--bot", default="", help="Existing GroupMe bot id (optional)")
    args = parser.parse_args()

    destination = args.thread or args.channel
    if not destination:
        print("pass --channel <id> or --thread <id> --parent <id>")
        return 1
    target_kind = "thread" if args.thread else "channel"
    if target_kind == "thread" and not args.parent:
        print("--thread needs --parent: the forum or channel that owns the webhook")
        return 1
    webhook_channel = args.parent or destination

    if bot.bridge_for_destination(destination):
        print(f"{destination} is already bridged; remove it first")
        return 1

    headers = {"Authorization": f"Bot {bot.DISCORD_TOKEN}"}
    async with aiohttp.ClientSession() as groupme, aiohttp.ClientSession(headers=headers) as discord:
        group = await bot.groupme_get_group(groupme, args.group)
        group_name = (group or {}).get("name") or ""

        topic_row = None
        if args.topic:
            topics = await bot.groupme_list_subgroups(groupme, args.group)
            topic_row = bot.find_topic(topics, args.topic)
            if topic_row is None:
                print("available topics:", [(t.get("topic"), t.get("id")) for t in topics] or "none")
                return 1

        # Main-chat bridges post through a GroupMe bot; topic bridges use the access
        # token and are polled, so they need no bot.
        bot_id = args.bot.strip()
        if not topic_row and not bot_id:
            bot_id, note = await bot.ensure_group_bot(
                groupme, args.group, await bot.fresh_bot_avatar(groupme)
            )
            print(note)
            if not bot_id:
                return 1

        async with discord.post(
            f"https://discord.com/api/v10/channels/{webhook_channel}/webhooks",
            json={"name": bot.WEBHOOK_NAME},
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                print("webhook creation failed:", payload)
                return 1

        conversation_id = str(topic_row["id"]) if topic_row else args.group
        conversation_name = (
            (topic_row.get("topic") or conversation_id) if topic_row else (group_name or args.group)
        )
        bot.save_bridge(
            conversation_id,
            destination,
            bot_id,
            conversation_name,
            conversation_type="topic" if topic_row else "group",
            group_id=args.group,
            group_name=group_name,
            target_kind=target_kind,
            parent_id=args.parent or None,
            webhook_id=int(payload["id"]),
            webhook_token=payload["token"],
            send_mode="user" if topic_row else "bot",
        )

        source = topic_row if topic_row else group
        last = ((source or {}).get("messages") or {}).get("last_message_id")
        if last:
            bot.set_last_seen(conversation_id, str(last))

    bridge = bot.bridge_for_conversation(conversation_id)
    print(
        "bridge:",
        {
            "conversation_id": bridge["conversation_id"],
            "type": bridge["conversation_type"],
            "group_id": bridge["group_id"],
            "channel_id": bridge["channel_id"],
            "target_kind": bridge["target_kind"],
            "parent_id": bridge["parent_id"] or "-",
            "bot_id": bridge["bot_id"] or "-",
            "send_mode": bridge["send_mode"],
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
