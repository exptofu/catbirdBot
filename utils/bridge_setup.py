"""Create or update a bridge without slash commands.

Creates the Discord webhook, ensures the GroupMe bot, resolves the topic if given,
and baselines both conversations so old history is not replayed. Mirrors `/bridge add`.

    python utils/bridge_setup.py --group 118478056 --channel 1558263681114968215
    python utils/bridge_setup.py --group 118478056 --channel 1558263681114968215 --topic cobb
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
    parser.add_argument("--channel", required=True, type=int, help="Discord channel id")
    parser.add_argument("--topic", default="", help="Topic name or id (optional)")
    parser.add_argument("--bot", default="", help="Existing GroupMe bot id (optional)")
    args = parser.parse_args()

    if bot.bridge_for_channel(args.channel):
        print(f"channel {args.channel} is already bridged; remove it first")
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

        bot_id = args.bot.strip()
        if not bot_id:
            bot_id, note = await bot.ensure_group_bot(
                groupme, args.group, await bot.fresh_bot_avatar(groupme)
            )
            print(note)
            if not bot_id:
                return 1

        async with discord.post(
            f"https://discord.com/api/v10/channels/{args.channel}/webhooks",
            json={"name": bot.WEBHOOK_NAME},
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                print("webhook creation failed:", payload)
                return 1

        bot.save_bridge(
            args.group,
            args.channel,
            bot_id,
            group_name,
            int(payload["id"]),
            payload["token"],
            topic_id=str(topic_row["id"]) if topic_row else "",
            topic_name=(topic_row.get("topic") or "") if topic_row else "",
            send_mode="user" if topic_row else "bot",
        )

        last = ((group or {}).get("messages") or {}).get("last_message_id")
        if last:
            bot.set_last_seen(args.group, str(last))
        if topic_row:
            topic_last = (topic_row.get("messages") or {}).get("last_message_id")
            if topic_last:
                bot.set_last_seen(str(topic_row["id"]), str(topic_last))

    bridge = bot.bridge_for_group(args.group)
    print(
        "bridge:",
        {
            "group_id": bridge["group_id"],
            "channel_id": bridge["channel_id"],
            "bot_id": bridge["bot_id"],
            "topic": bridge["topic_name"] or "-",
            "send_mode": bridge["send_mode"],
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
