"""Compare the slash commands Discord has registered against the ones in bot.py.

    python utils/check_commands.py          # read-only report
    python utils/check_commands.py --fix    # delete the stale global command set

A `/bridge` subcommand that appears twice in Discord, or that still asks for a
parameter the code no longer has, means Discord is holding an old registration.
A guild resync cannot delete a *global* copy, so `--fix` clears the global set;
restart the bot afterwards so it publishes the guild-scoped commands.

Discord's desktop client caches the list too: press Ctrl+R after a change.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import bot  # noqa: E402

API = "https://discord.com/api/v10"
SUBCOMMAND_TYPES = {1, 2}


def local_signatures() -> set[str]:
    """`bridge add(group_id, topic, bot_id)`-style signatures from the local tree."""
    signatures: set[str] = set()
    for command in bot.client.tree.get_commands():
        if isinstance(command, bot.app_commands.Group):
            for child in command.commands:
                params = ", ".join(sorted(parameter.name for parameter in child.parameters))
                signatures.add(f"{command.name} {child.name}({params})")
        else:
            signatures.add(f"{command.name}()")
    return signatures


def registered_signatures(command: dict) -> set[str]:
    """Same shape, built from a Discord application-command payload."""
    name = command.get("name") or "?"
    options = command.get("options") or []
    subcommands = [option for option in options if option.get("type") in SUBCOMMAND_TYPES]
    if not subcommands:
        params = ", ".join(sorted(option.get("name", "?") for option in options))
        return {f"{name}({params})"}
    signatures = set()
    for subcommand in subcommands:
        nested = subcommand.get("options") or []
        params = ", ".join(sorted(option.get("name", "?") for option in nested))
        signatures.add(f"{name} {subcommand.get('name')}({params})")
    return signatures


async def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect registered slash commands")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="delete every global command (the bot re-publishes guild commands on restart)",
    )
    args = parser.parse_args()

    if not bot.DISCORD_TOKEN:
        print("DISCORD_TOKEN is empty; fill in .env first")
        return 1

    headers = {"Authorization": f"Bot {bot.DISCORD_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.get(f"{API}/applications/@me") as response:
            application = await response.json()
            if response.status >= 400:
                print("could not read the application (bad token?):", application)
                return 1
        application_id = application["id"]

        async def fetch(path: str) -> list[dict]:
            async with http.get(f"{API}/applications/{application_id}{path}") as response:
                payload = await response.json()
                if response.status >= 400:
                    print(f"GET {path} failed:", payload)
                    return []
                return payload

        global_commands = await fetch("/commands")
        guild_commands: list[dict] = []
        if bot.DISCORD_GUILD_ID:
            guild_commands = await fetch(f"/guilds/{bot.DISCORD_GUILD_ID}/commands")

    expected = local_signatures()
    print(f"code (bot.py): {len(expected)} command(s)")
    for signature in sorted(expected):
        print(f"  /{signature}")

    seen: dict[str, list[str]] = {}
    for scope, commands in (
        ("global", global_commands),
        (f"guild {bot.DISCORD_GUILD_ID}" if bot.DISCORD_GUILD_ID else "", guild_commands),
    ):
        if not scope:
            continue
        print(f"\nregistered {scope}: {len(commands)} command(s)")
        for command in commands:
            for signature in sorted(registered_signatures(command)):
                seen.setdefault(signature, []).append(scope)
                print(f"  /{signature}")

    stale = {signature: scopes for signature, scopes in seen.items() if signature not in expected}
    missing = expected - set(seen)
    if stale:
        print("\nSTALE (registered but no longer in the code):")
        for signature, scopes in sorted(stale.items()):
            print(f"  /{signature} — in {', '.join(scopes)}")
    if missing:
        print("\nMISSING (in the code but not registered):")
        for signature in sorted(missing):
            print(f"  /{signature}")
    if not stale and not missing:
        print("\nregistered commands match the code")
        return 0

    if not global_commands:
        print("\nrestart the bot so it resyncs the guild commands")
        return 0
    if not args.fix:
        print("\nrun again with --fix to delete the global command set, then restart the bot")
        return 0

    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.put(f"{API}/applications/{application_id}/commands", json=[]) as response:
            if response.status >= 400:
                print("could not delete the global commands:", await response.json())
                return 1
    print(f"\ndeleted {len(global_commands)} global command(s); restart the bot to republish")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
