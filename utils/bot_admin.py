"""GroupMe bot maintenance for Catbird.

    python utils/bot_admin.py list
    python utils/bot_admin.py ensure --group 12345678
    python utils/bot_admin.py repair
    python utils/bot_admin.py resync-avatars

`list` shows every bot on the account with its callback and whether the avatar URL
actually resolves (GroupMe deletes hosted avatars when their bot is destroyed, and
the URL then returns 403). `repair` rebuilds bots for bridged groups whose stored id
is no longer on the account. `resync-avatars` recreates every bridged bot with a
fresh upload of the configured avatar, since GroupMe has no bot update endpoint.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import bot  # noqa: E402


async def cmd_list(session: aiohttp.ClientSession) -> None:
    bots = await bot.groupme_list_bots(session)
    if not bots:
        print("no bots on this account")
        return
    for entry in bots:
        avatar = entry.get("avatar_url") or ""
        status = "-"
        if avatar:
            try:
                async with session.get(avatar, timeout=aiohttp.ClientTimeout(total=15)) as probe:
                    status = probe.status
            except aiohttp.ClientError:
                status = "error"
        print(
            f"{entry['bot_id']} | group {entry['group_id']} | {entry.get('name')} | "
            f"avatar {status} | callback {entry.get('callback_url') or '-'}"
        )
    print(f"\n{len(bots)} bot(s); callback URL now: {bot.callback_url() or '(none)'}")


async def cmd_ensure(session: aiohttp.ClientSession, group_id: str) -> None:
    bot_id, note = await bot.ensure_group_bot(
        session, group_id, await bot.fresh_bot_avatar(session)
    )
    print(note or "no bot available")
    if not bot_id:
        return
    bridge = bot.bridge_for_group(group_id)
    if bridge is None:
        print(f"bot_id={bot_id} (no bridge yet; pass it to /bridge add)")
        return
    bot.save_bridge(
        group_id,
        int(bridge["channel_id"]),
        bot_id,
        bridge["group_name"],
        topic_id=bridge["topic_id"],
        topic_name=bridge["topic_name"],
        send_mode=bridge["send_mode"] or "bot",
    )
    print(f"bridge for {group_id} now uses {bot_id}")


async def cmd_repair(session: aiohttp.ClientSession) -> None:
    known = {str(entry.get("bot_id")) for entry in await bot.groupme_list_bots(session)}
    for bridge in bot.list_bridges():
        stored = bridge["bot_id"] or ""
        if stored and stored in known:
            print(f"{bridge['group_id']}: bot {stored} ok")
            continue
        try:
            new_id = await bot.recover_bot_id(session, bridge)
        except bot.GroupMeError as error:
            print(f"{bridge['group_id']}: repair failed: {error}")
            continue
        print(f"{bridge['group_id']}: replaced {stored or '(none)'} with {new_id}")


async def cmd_resync_avatars(session: aiohttp.ClientSession) -> None:
    for bridge in bot.list_bridges():
        group_id = bridge["group_id"]
        if bridge["bot_id"]:
            try:
                await bot.groupme_destroy_bot(session, bridge["bot_id"])
            except bot.GroupMeError as error:
                print(f"{group_id}: could not remove {bridge['bot_id']}: {error}")
                continue
        avatar = await bot.fresh_bot_avatar(session)
        created = await bot.groupme_create_bot(
            session, group_id, bot.GROUPME_BOT_NAME, bot.callback_url(), avatar
        )
        new_id = str(created.get("bot_id") or "")
        if not new_id:
            print(f"{group_id}: bot recreation failed")
            continue
        bot.save_bridge(
            group_id,
            int(bridge["channel_id"]),
            new_id,
            bridge["group_name"],
            topic_id=bridge["topic_id"],
            topic_name=bridge["topic_name"],
            send_mode=bridge["send_mode"] or "bot",
        )
        print(f"{group_id}: {new_id} | avatar {created.get('avatar_url') or '(none)'}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="GroupMe bot maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every bot with its avatar status")
    ensure = sub.add_parser("ensure", help="create or reuse a bot for one group")
    ensure.add_argument("--group", required=True, help="GroupMe group id")
    sub.add_parser("repair", help="replace bot ids that no longer exist")
    sub.add_parser("resync-avatars", help="recreate every bridged bot with the configured avatar")
    args = parser.parse_args()

    if not bot.GROUPME_ACCESS_TOKEN:
        print("GROUPME_ACCESS_TOKEN is not set")
        return 1

    async with aiohttp.ClientSession() as session:
        if args.command == "list":
            await cmd_list(session)
        elif args.command == "ensure":
            await cmd_ensure(session, args.group)
        elif args.command == "repair":
            await cmd_repair(session)
        elif args.command == "resync-avatars":
            await cmd_resync_avatars(session)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
