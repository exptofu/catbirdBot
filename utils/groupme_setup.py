"""GroupMe helper for Catbird setup.

List the groups and bots on a GroupMe account, and create or update the bot that
Catbird uses to post into a group.

    python utils/groupme_setup.py groups
    python utils/groupme_setup.py bots
    python utils/groupme_setup.py create-bot --group-id 12345678 --callback-url https://example.com/groupme/callback
    python utils/groupme_setup.py set-callback --bot-id abc123 --callback-url https://example.com/groupme/callback
    python utils/groupme_setup.py delete-bot --bot-id abc123

Reads GROUPME_ACCESS_TOKEN from the environment or from .env in the parent of
this directory. The token comes from https://dev.groupme.com/applications.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API = os.environ.get("GROUPME_API_BASE", "https://api.groupme.com/v3").rstrip("/")
TOKEN = os.environ.get("GROUPME_ACCESS_TOKEN", "").strip()
DEFAULT_BOT_NAME = "GA Birding Discord"


def headers() -> dict[str, str]:
    return {"X-Access-Token": TOKEN}


async def call(session: aiohttp.ClientSession, method: str, path: str, **kwargs):
    async with session.request(
        method, f"{API}{path}", headers=headers(), timeout=aiohttp.ClientTimeout(total=30), **kwargs
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise SystemExit(f"{method} {path} failed with {response.status}: {body[:300]}")
        if not body.strip():
            return None
        return json.loads(body).get("response")


async def list_groups(session: aiohttp.ClientSession) -> list[dict]:
    return await call(session, "GET", "/groups", params={"per_page": 100, "omit": "memberships"}) or []


async def list_bots(session: aiohttp.ClientSession) -> list[dict]:
    return await call(session, "GET", "/bots") or []


async def create_bot(
    session: aiohttp.ClientSession, group_id: str, name: str, callback_url: str, avatar_url: str
) -> dict:
    form = {
        "bot[name]": name,
        "bot[group_id]": group_id,
        "bot[active]": "true",
        "bot[dm_notification]": "false",
    }
    if callback_url:
        form["bot[callback_url]"] = callback_url
    if avatar_url:
        form["bot[avatar_url]"] = avatar_url
    return await call(session, "POST", "/bots", data=form)


async def main() -> None:
    parser = argparse.ArgumentParser(description="GroupMe setup helper for Catbird")
    parser.add_argument(
        "command", choices=["groups", "bots", "create-bot", "set-callback", "delete-bot"]
    )
    parser.add_argument("--group-id", help="GroupMe group id")
    parser.add_argument("--bot-id", help="GroupMe bot id")
    parser.add_argument("--name", default=DEFAULT_BOT_NAME, help="Bot display name")
    parser.add_argument("--callback-url", default="", help="Public URL for GroupMe bot callbacks")
    parser.add_argument("--avatar-url", default="", help="Bot avatar image URL")
    args = parser.parse_args()

    if not TOKEN:
        raise SystemExit("GROUPME_ACCESS_TOKEN is not set (copy .env.example to .env).")

    async with aiohttp.ClientSession() as session:
        if args.command == "groups":
            groups = await list_groups(session)
            print(f"{len(groups)} group(s):")
            for group in groups:
                print(f"  {group['id']:>12}  {group.get('name', '')}")
            print("\nUse the id with /bridge add in Discord.")
        elif args.command == "bots":
            bots = await list_bots(session)
            print(f"{len(bots)} bot(s):")
            for bot in bots:
                print(
                    f"  {bot.get('bot_id', ''):>12}  group={bot.get('group_id', ''):>12}  "
                    f"name={bot.get('name', '')}  callback={bot.get('callback_url') or '(none)'}"
                )
        elif args.command == "create-bot":
            if not args.group_id:
                raise SystemExit("--group-id is required for create-bot.")
            bot = await create_bot(session, args.group_id, args.name, args.callback_url, args.avatar_url)
            if not bot:
                raise SystemExit("GroupMe did not return a bot.")
            print(f"bot_id={bot['bot_id']}")
            print(f"group_id={bot['group_id']}")
            print(f"callback_url={bot.get('callback_url') or '(none)'}")
            print("\nUse bot_id with /bridge add in Discord.")
        elif args.command == "set-callback":
            if not args.bot_id or not args.callback_url:
                raise SystemExit("--bot-id and --callback-url are required for set-callback.")
            await call(
                session,
                "POST",
                f"/bots/{args.bot_id}",
                data={"bot[callback_url]": args.callback_url, "bot[active]": "true"},
            )
            print(f"Updated callback for bot {args.bot_id}.")
        elif args.command == "delete-bot":
            if not args.bot_id:
                raise SystemExit("--bot-id is required for delete-bot.")
            await call(session, "POST", "/bots/destroy", data={"bot_id": args.bot_id})
            print(f"Deleted bot {args.bot_id}.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
