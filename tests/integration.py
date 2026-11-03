import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

POSTED_BOT = []
POSTED_MESSAGE = []
CREATED_BOTS = []
UPLOADS = []
CALLS = []
DELETED_MESSAGES = []
LATEST_MESSAGES = []


def build_big_png():
    import io
    import os as _os

    from PIL import Image

    image = Image.frombytes("RGB", (3000, 2000), _os.urandom(3000 * 2000 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


BIG_PNG = build_big_png()


def local_png():
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 80, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


async def bot_post(request):
    payload = await request.json()
    if payload.get("bot_id") == "stale-bot":
        return web.json_response(
            {"meta": {"code": 404, "errors": ["The requested path does not exist"]}, "response": None},
            status=404,
        )
    POSTED_BOT.append(payload)
    return web.Response(status=201)


async def bots_create(request):
    form = dict(await request.post())
    CREATED_BOTS.append(form)
    if form.get("bot[group_id]") == "NOAVATAR" and form.get("bot[avatar_url]"):
        return web.json_response(
            {"meta": {"code": 400, "errors": ["avatar_url: invalid origin"]}, "response": None},
            status=400,
        )
    if form.get("bot[group_id]") == "MAXBOTS":
        return web.json_response(
            {"meta": {"code": 400, "errors": ["group has too many bots"]}, "response": None},
            status=400,
        )
    if form.get("bot[group_id]") == "UNAUTHORIZED":
        return web.json_response(
            {"meta": {"code": 401, "errors": ["unauthorized"]}, "response": None},
            status=401,
        )
    return web.json_response(
        {
            "response": {
                "bot": {
                    "bot_id": "new-bot-id",
                    "group_id": form.get("bot[group_id]"),
                    "name": form.get("bot[name]"),
                    "callback_url": form.get("bot[callback_url]"),
                    "avatar_url": form.get("bot[avatar_url]"),
                }
            }
        }
    )


async def bots_list(request):
    return web.json_response(
        {
            "response": [
                {"bot_id": "old-bot", "group_id": "G1", "name": "Catbird Bridge", "callback_url": "https://old.example"},
                {"bot_id": "new-bot-id", "group_id": "G1", "name": "Catbird Bridge", "callback_url": "https://new.example"},
                {"bot_id": "max-bot", "group_id": "MAXBOTS", "name": "Catbird Bridge", "callback_url": "https://elsewhere.example"},
            ]
        }
    )


async def bots_destroy(request):
    form = dict(await request.post())
    CREATED_BOTS.append({"destroyed": form.get("bot_id")})
    return web.Response(status=200)


async def message_post(request):
    body = await request.json()
    POSTED_MESSAGE.append({"group_id": request.match_info["group_id"], **body["message"]})
    return web.json_response({"response": {"message": {"id": "9001", **body["message"]}}})


async def pictures(request):
    import io

    from PIL import Image

    raw = await request.read()
    with Image.open(io.BytesIO(raw)) as image:
        dims = image.size
    UPLOADS.append({"content_type": request.content_type, "length": len(raw), "dims": dims})
    return web.json_response({"payload": {"url": "https://i.groupme.com/test.jpg"}})


async def cdn(request):
    return web.Response(body=BIG_PNG, content_type="image/png")


async def message_delete(request):
    DELETED_MESSAGES.append(
        (request.match_info["group_id"], request.match_info["message_id"])
    )
    return web.Response(status=204)


async def group_messages(request):
    gid = request.match_info["group_id"]
    since = request.query.get("since_id")
    CALLS.append(f"{gid}:{since}")
    if since == "5":
        return web.Response(status=304)
    if not since:
        return web.json_response(
            {"response": {"count": len(LATEST_MESSAGES), "messages": list(LATEST_MESSAGES)}}
        )
    if since == "6":
        return web.json_response(
            {
                "response": {
                    "count": 3,
                    "messages": [
                        {"id": "12", "group_id": gid, "sender_type": "user", "user_id": "u1", "name": "Zed", "text": "third", "created_at": 1700000003},
                        {"id": "11", "group_id": gid, "sender_type": "bot", "user_id": "b1", "name": "Some Bot", "text": "bot noise", "created_at": 1700000002},
                        {"id": "10", "group_id": gid, "sender_type": "user", "user_id": "u2", "name": "Amy", "text": "first", "created_at": 1700000001},
                    ],
                }
            }
        )
    return web.json_response({"response": {"count": 0, "messages": []}})


async def group(request):
    return web.json_response(
        {"response": {"id": request.match_info["group_id"], "name": "Testers", "messages": {"last_message_id": "42"}}}
    )


async def group_index(request):
    return web.json_response(
        {
            "response": [
                {"id": "G1", "name": "Testers"},
                {"id": "G2", "name": "Topic Group"},
            ]
        }
    )


async def subgroups(request):
    return web.json_response(
        {
            "response": [
                {"id": 118478062, "parent_id": 118478056, "topic": "cobb", "messages": {"count": 4, "last_message_id": "777"}},
                {"id": 118478063, "parent_id": 118478056, "topic": "dekalb", "messages": {"count": 0}},
            ]
        }
    )


async def flaky(request):
    if not CALLS or CALLS[-1] != "flaky:attempt":
        CALLS.append("flaky:attempt")
        return web.Response(status=500, text="boom")
    return web.json_response({"response": None})


class Author:
    display_name = "Ann"
    name = "ann"


class Attachment:
    def __init__(self, url, content_type, filename, size=None):
        self.url = url
        self.content_type = content_type
        self.filename = filename
        self.size = len(BIG_PNG) if size is None else size


class Message:
    id = 999
    content = "hello group"
    mentions = []
    role_mentions = []
    channel_mentions = []
    reference = None
    created_at = datetime.now(timezone.utc)
    author = Author()
    attachments = [
        Attachment("http://127.0.0.1:8731/cdn/a.png", "image/png", "a.png"),
        Attachment("https://cdn.discordapp.com/b.pdf", "application/pdf", "b.pdf"),
    ]


class BotMessage(Message):
    """A second Discord message, so its outbound links are easy to isolate."""

    id = 1001


async def main():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/bots/post", bot_post)
    app.router.add_post("/bots", bots_create)
    app.router.add_get("/bots", bots_list)
    app.router.add_post("/bots/destroy", bots_destroy)
    app.router.add_post("/groups/{group_id}/messages", message_post)
    app.router.add_get("/groups/{group_id}/messages", group_messages)
    app.router.add_delete("/conversations/{group_id}/messages/{message_id}", message_delete)
    app.router.add_get("/groups/{group_id}/subgroups", subgroups)
    app.router.add_get("/groups/{group_id}", group)
    app.router.add_get("/groups", group_index)
    app.router.add_post("/images/pictures", pictures)
    app.router.add_get("/cdn/a.png", cdn)
    app.router.add_get("/flaky", flaky)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8731)
    await site.start()

    avatar_file = os.path.join(tempfile.mkdtemp(), "avatar.png")
    with open(avatar_file, "wb") as handle:
        handle.write(local_png())
    os.environ["GROUPME_BOT_AVATAR_PATH"] = avatar_file

    os.environ["DISCORD_TOKEN"] = "dummy"
    os.environ["GROUPME_ACCESS_TOKEN"] = "token"
    os.environ["GROUPME_MODE"] = "webhook"
    os.environ["GROUPME_API_BASE"] = "http://127.0.0.1:8731"
    os.environ["GROUPME_IMAGE_API"] = "http://127.0.0.1:8731/images"
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "catbird_it.db")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import aiohttp
    import bot

    async with aiohttp.ClientSession() as session:
        UPLOADS.clear()
        avatar_url = await bot.fresh_bot_avatar(session)
        assert avatar_url == "https://i.groupme.com/test.jpg", avatar_url
        assert UPLOADS[0]["content_type"] == "image/png", UPLOADS[0]
        assert UPLOADS[0]["length"] == os.path.getsize(avatar_file), UPLOADS[0]

        created = await bot.groupme_create_bot(session, "G1", "Catbird Bridge", "https://new.example")
        assert created["bot_id"] == "new-bot-id", created
        assert CREATED_BOTS[0]["bot[name]"] == "Catbird Bridge", CREATED_BOTS[0]
        assert CREATED_BOTS[0]["bot[callback_url]"] == "https://new.example", CREATED_BOTS[0]
        assert (await bot.groupme_find_bot(session, "G1", "https://new.example"))["bot_id"] == "new-bot-id"
        assert (await bot.groupme_find_bot(session, "G1"))["bot_id"] == "old-bot"
        assert await bot.groupme_find_bot(session, "OTHER") is None
        await bot.groupme_destroy_bot(session, "new-bot-id")
        assert CREATED_BOTS[-1] == {"destroyed": "new-bot-id"}, CREATED_BOTS[-1]

        # a refused avatar is retried without one instead of failing the bridge
        CREATED_BOTS.clear()
        bot_id, note = await bot.ensure_group_bot(
            session, "NOAVATAR", "https://i.groupme.com/x.png"
        )
        assert bot_id == "new-bot-id", (bot_id, note)
        assert len(CREATED_BOTS) == 2, CREATED_BOTS
        assert CREATED_BOTS[0]["bot[avatar_url]"] == "https://i.groupme.com/x.png"
        assert "bot[avatar_url]" not in CREATED_BOTS[1], CREATED_BOTS[1]

        # a group whose bot creation fails reuses the bot the account already has,
        # and the note carries GroupMe's reason instead of a generic message
        CREATED_BOTS.clear()
        bot_id, note = await bot.ensure_group_bot(session, "MAXBOTS")
        assert bot_id == "max-bot", (bot_id, note)
        assert "too many bots" in note, note

        # 401 from POST /bots is how GroupMe answers a topic id or a bad token
        bot_id, note = await bot.ensure_group_bot(session, "UNAUTHORIZED")
        assert bot_id == "", (bot_id, note)
        assert "401" in note and "unauthorized" in note, note

        # ...and the account's group list is what tells the two apart
        assert await bot.groupme_owns_group(session, "G1") is True
        assert await bot.groupme_owns_group(session, "59716270") is False

        topics = await bot.groupme_list_subgroups(session, "G1")
        assert [t["topic"] for t in topics] == ["cobb", "dekalb"]
        assert bot.find_topic(topics, "cobb")["id"] == 118478062
        assert bot.find_topic(topics, "118478062")["id"] == 118478062
        assert bot.find_topic(topics, "dal") is None

        await bot.groupme_request(session, "GET", "/flaky")
        assert CALLS.count("flaky:attempt") == 1

        assert await bot.groupme_list_messages(session, "G1", since_id="5") == []

        bot.save_bridge("G1", 555, "bot-1", "Testers", group_name="Testers")
        bot.save_bridge(
            "T1",
            777,
            "",
            "cobb",
            conversation_type="topic",
            group_id="G2",
            group_name="Topic Group",
            target_kind="thread",
            parent_id=700,
            send_mode="user",
        )
        topic_bridge = bot.bridge_for_conversation("T1")
        assert bot.bridge_for_conversation("T1")["conversation_id"] == "T1"
        assert bot.bridge_for_conversation("G2") is None
        assert bot.bridge_for_destination(777)["group_id"] == "G2"
        assert bot.outbound_conversation_id(topic_bridge) == "T1"
        assert bot.outbound_conversation_id(bot.bridge_for_conversation("G1")) == "G1"

        targets = dict(bot.poll_targets())
        # topics are always polled; a main-chat bridge with a bot is callback-driven
        assert "T1" in targets and "G1" not in targets, targets
        assert "G2" not in targets, targets

        await bot.poll_conversation(session, "G1", bot.bridge_for_conversation("G1"))
        assert bot.last_seen("G1") == "42"

        relayed = []

        async def fake_process(message, *, source):
            relayed.append(message["id"])

        original_process = bot.client.process_groupme_message
        bot.client.process_groupme_message = fake_process
        bot.set_last_seen("G1", "6")
        await bot.poll_conversation(session, "G1", bot.bridge_for_conversation("G1"))
        assert relayed == ["10", "11", "12"], relayed
        assert bot.last_seen("G1") == "12"
        bot.client.process_groupme_message = original_process

        # bot mode: text first, then one picture per image, both re-hosted
        POSTED_BOT.clear()
        UPLOADS.clear()
        await bot.relay_discord_message(session, Message(), bot.bridge_for_conversation("G1"))
        assert POSTED_BOT[0] == {"bot_id": "bot-1", "text": "Ann: hello group\nhttps://cdn.discordapp.com/b.pdf"}, POSTED_BOT[0]
        assert POSTED_BOT[1] == {"bot_id": "bot-1", "text": "Ann:", "picture_url": "https://i.groupme.com/test.jpg"}, POSTED_BOT[1]
        assert UPLOADS[0]["content_type"] == "image/jpeg", UPLOADS[0]
        assert max(UPLOADS[0]["dims"]) <= 2048, UPLOADS[0]

        # a stale bot id is repaired from the account and the post retried
        bot.save_bridge("G1", 555, "stale-bot", "Testers", group_name="Testers")
        POSTED_BOT.clear()
        await bot.relay_discord_message(session, Message(), bot.bridge_for_conversation("G1"))
        assert POSTED_BOT and POSTED_BOT[0]["bot_id"] == "old-bot", POSTED_BOT
        assert bot.bridge_for_conversation("G1")["bot_id"] == "old-bot"

        # a failed upload is reported in the message instead of posting a URL
        original_upload = bot.groupme_upload_image

        async def failing_upload(*args, **kwargs):
            raise bot.GroupMeError("upload down")

        bot.groupme_upload_image = failing_upload
        try:
            POSTED_BOT.clear()
            await bot.relay_discord_message(session, Message(), bot.bridge_for_conversation("G1"))
            assert len(POSTED_BOT) == 1, POSTED_BOT
            assert POSTED_BOT[0]["text"].endswith("[1 image(s) could not be relayed]"), POSTED_BOT[0]
        finally:
            bot.groupme_upload_image = original_upload

        # topic mode: one message carrying re-hosted attachments
        POSTED_MESSAGE.clear()
        UPLOADS.clear()
        await bot.relay_discord_message(session, Message(), bot.bridge_for_conversation("T1"))
        assert len(POSTED_MESSAGE) == 1, POSTED_MESSAGE
        sent = POSTED_MESSAGE[0]
        assert sent["text"].startswith("Ann: hello group")
        assert sent["attachments"] == [{"type": "image", "url": "https://i.groupme.com/test.jpg"}], sent
        assert UPLOADS[0]["content_type"] == "image/jpeg", UPLOADS[0]
        assert max(UPLOADS[0]["dims"]) <= 2048, UPLOADS[0]
        assert "9001" in bot.RECENT_OUTBOUND

        # a group that refuses bots posts into the main chat with the access token,
        # and is polled because there is no bot to receive callbacks
        bot.save_bridge(
            "G3",
            888,
            "",
            "No Bots",
            conversation_type="group",
            group_name="No Bots",
            send_mode="user",
        )
        assert "G3" in dict(bot.poll_targets()), bot.poll_targets()
        assert bot.bridge_for_conversation("G3")["send_mode"] == "user"
        POSTED_MESSAGE.clear()
        UPLOADS.clear()
        await bot.relay_discord_message(session, Message(), bot.bridge_for_conversation("G3"))
        assert len(POSTED_MESSAGE) == 1, POSTED_MESSAGE
        assert POSTED_MESSAGE[0]["group_id"] == "G3", POSTED_MESSAGE[0]
        assert POSTED_MESSAGE[0]["text"].startswith("Ann: hello group"), POSTED_MESSAGE[0]
        assert UPLOADS[0]["content_type"] == "image/jpeg", UPLOADS[0]
        # token posts return their message id, so the Discord message can be deleted later
        assert [row["groupme_id"] for row in bot.outbound_messages(Message.id)] == [
            "9001",
            "9001",
        ], bot.outbound_messages(Message.id)

        # /bots/post returns nothing, so bot posts are matched back by text and picture
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        LATEST_MESSAGES[:] = [
            {
                "id": "9102",
                "group_id": "G1",
                "sender_type": "bot",
                "user_id": "b1",
                "name": "Catbird Bridge",
                "text": "Ann:",
                "created_at": now_epoch,
                "attachments": [{"type": "image", "url": "https://i.groupme.com/test.jpg"}],
            },
            {
                "id": "9101",
                "group_id": "G1",
                "sender_type": "bot",
                "user_id": "b1",
                "name": "Catbird Bridge",
                "text": "Ann: hello group\nhttps://cdn.discordapp.com/b.pdf",
                "created_at": now_epoch,
            },
        ]
        POSTED_BOT.clear()
        bot.save_bridge("G1", 555, "bot-1", "Testers", group_name="Testers")
        await bot.relay_discord_message(session, BotMessage(), bot.bridge_for_conversation("G1"))
        assert sorted(row["groupme_id"] for row in bot.outbound_messages(BotMessage.id)) == [
            "9101",
            "9102",
        ], bot.outbound_messages(BotMessage.id)

        # deleting the Discord message deletes every GroupMe copy it produced
        bot.client.groupme_session = session
        DELETED_MESSAGES.clear()
        await bot.client.sync_discord_deletion(BotMessage.id)
        assert sorted(DELETED_MESSAGES) == [("G1", "9101"), ("G1", "9102")], DELETED_MESSAGES
        assert bot.outbound_messages(BotMessage.id) == [], bot.outbound_messages(BotMessage.id)

        # thread destinations pass the thread id; plain channels must omit it
        webhook_calls = []

        class FakeWebhook:
            async def delete_message(self, message_id, **kwargs):
                webhook_calls.append((message_id, kwargs))

        async def fake_get_webhook(bridge, *, refresh=False):
            return FakeWebhook()

        bot.client.get_webhook = fake_get_webhook
        assert await bot.client.delete_discord_copy(
            bot.bridge_for_conversation("G1"), "4242"
        )
        assert webhook_calls[-1] == (4242, {}), webhook_calls
        assert await bot.client.delete_discord_copy(topic_bridge, "4243")
        assert webhook_calls[-1][0] == 4243, webhook_calls
        assert webhook_calls[-1][1]["thread"].id == 777, webhook_calls

        # a GroupMe message missing from the newest page loses its Discord copy
        removed_copies = []

        async def fake_delete_copy(bridge, discord_id):
            removed_copies.append(discord_id)
            return True

        bot.client.delete_discord_copy = fake_delete_copy
        LATEST_MESSAGES[:] = [
            {
                "id": "9200",
                "group_id": "G1",
                "sender_type": "user",
                "user_id": "u1",
                "name": "Ann",
                "text": "still here",
                "created_at": now_epoch,
            }
        ]
        bot.add_link(
            bot.LINK_GROUPME,
            "G1",
            555,
            "9300",
            "4242",
            datetime.now(timezone.utc),
        )
        await bot.client.reconcile_groupme_deletions(
            session, bot.bridge_for_conversation("G1")
        )
        assert removed_copies == ["4242"], removed_copies
        assert not bot.link_exists(bot.LINK_GROUPME, "G1", "9300")

        # GroupMe keeps a tombstone carrying deleted_at, so the id is still returned
        LATEST_MESSAGES[:] = [
            {
                "id": "9400",
                "group_id": "G1",
                "sender_type": "user",
                "user_id": "u1",
                "name": "Ann",
                "text": "This message was deleted",
                "deleted_at": now_epoch,
                "created_at": now_epoch,
            }
        ]
        bot.add_link(
            bot.LINK_GROUPME, "G1", 555, "9400", "4243", datetime.now(timezone.utc)
        )
        await bot.client.reconcile_groupme_deletions(
            session, bot.bridge_for_conversation("G1")
        )
        assert removed_copies[-1] == "4243", removed_copies
        assert not bot.link_exists(bot.LINK_GROUPME, "G1", "9400")

        # ...and posts a system event naming the deleted message, which is immediate
        bot.add_link(
            bot.LINK_GROUPME, "G1", 555, "9500", "4244", datetime.now(timezone.utc)
        )
        await bot.client.process_groupme_message(
            {
                "id": "9501",
                "group_id": "G1",
                "sender_type": "system",
                "user_id": "system",
                "name": "GroupMe",
                "system": True,
                "text": "A message was deleted.",
                "created_at": now_epoch,
                "event": {"type": "message.deleted", "data": {"message_id": "9500"}},
            },
            source="test",
        )
        assert removed_copies[-1] == "4244", removed_copies
        assert not bot.link_exists(bot.LINK_GROUPME, "G1", "9500")

        small = bot.compress_image(b"not-an-image", "image/png", "small.png")
        assert small == (b"not-an-image", "image/png"), small

    await runner.cleanup()
    print("integration ok")


asyncio.run(main())
