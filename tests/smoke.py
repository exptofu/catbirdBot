import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["DISCORD_TOKEN"] = "dummy"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "catbird_smoke.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

# bridge + cursor + link round trip
bot.save_bridge("12345678", 111, "botabc", "Test Group")
assert bot.bridge_for_group("12345678")["channel_id"] == 111
assert bot.bridge_for_channel(111)["bot_id"] == "botabc"
assert bot.bridge_for_channel(999) is None
assert [row["group_id"] for row in bot.list_bridges()] == ["12345678"]

bot.set_last_seen("12345678", "900")
assert bot.last_seen("12345678") == "900"

bot.add_link(bot.LINK_GROUPME, "12345678", 111, "555", None, datetime.now(timezone.utc))
assert bot.link_exists(bot.LINK_GROUPME, "12345678", "555")
assert bot.link_exists(bot.LINK_GROUPME, "12345678", "555")
assert not bot.link_exists("nope", "12345678", "555")

bot.delete_bridge("12345678")
assert bot.bridge_for_group("12345678") is None and bot.last_seen("12345678") is None

# helpers
assert bot.parse_group_id("https://groupme.com/join_group/12345678/AbCdEf/") == "12345678"
assert bot.parse_group_id(" 99887766 ") == "99887766"
assert len(bot.truncate("x" * 1200)) == 1000 and bot.truncate("x" * 1200).endswith("…")

assert bot.is_groupme_bot_message({"system": True, "user_id": "1", "sender_type": "user"})
assert bot.is_groupme_bot_message({"sender_type": "bot", "user_id": "1"})
assert bot.is_groupme_bot_message({"user_id": None, "name": "someone"})
assert not bot.is_groupme_bot_message({"sender_type": "user", "user_id": "1", "name": "Ann"})

groupme_message = {
    "text": "hello",
    "attachments": [
        {"type": "image", "url": "https://i.groupme.com/a"},
        {"type": "location", "name": "HQ", "lat": "1.5", "lng": "2.5"},
    ],
}
assert bot.groupme_image_urls(groupme_message) == ["https://i.groupme.com/a"]
assert "maps.google.com" in bot.groupme_message_text(groupme_message)
assert bot.groupme_message_text({"attachments": [{"type": "image", "url": "x"}]}) == ""

assert bot.sanitize_webhook_name("clyde") == "clyde (GroupMe)"
assert bot.sanitize_webhook_name("  Ann  ") == "Ann"
assert bot.sanitize_webhook_name("") == "GroupMe user"

# discord text rendering
class FakeMember:
    def __init__(self, id, name):
        self.id = id
        self.display_name = name
        self.name = name


class FakeRole:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class FakeChannel:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class FakeMessage:
    id = 1


FakeMessage.content = "hey <@42> <@&7> in <#9> <:bird:123> <a:wave:456> 🎉 done"
FakeMessage.mentions = [FakeMember(42, "Ann")]
FakeMessage.role_mentions = [FakeRole(7, "Mods")]
FakeMessage.channel_mentions = [FakeChannel(9, "general")]

rendered = bot.render_discord_content(FakeMessage)
assert rendered == "hey @Ann @Mods in #general done", rendered

# command tree wiring
names = sorted(c.name for c in bot.client.tree.get_commands())
assert names == ["bridge"], names
print("smoke ok")
