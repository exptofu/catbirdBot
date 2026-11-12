import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["DISCORD_TOKEN"] = "dummy"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "catbird_smoke.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

# bridge + cursor + link round trip
bot.save_bridge("12345678", 111, "botabc", "Test Group", group_name="Test Group")
assert bot.bridge_for_conversation("12345678")["channel_id"] == 111
assert bot.bridge_for_destination(111)["bot_id"] == "botabc"
assert bot.bridge_for_destination(999) is None
assert [row["conversation_id"] for row in bot.list_bridges()] == ["12345678"]
assert bot.webhook_channel_id(bot.bridge_for_conversation("12345678")) == 111

# a topic of the same group is a separate bridge with its own destination
bot.save_bridge(
    "99887",
    222,
    "",
    "cobb",
    conversation_type="topic",
    group_id="12345678",
    group_name="Test Group",
    target_kind="thread",
    parent_id=333,
    send_mode="user",
)
topic = bot.bridge_for_conversation("99887")
assert topic["conversation_type"] == "topic" and topic["group_id"] == "12345678"
assert bot.conversation_label(topic) == "#cobb"
assert bot.destination_label(topic) == "<#222> (post)"
# the webhook for a forum post lives on the parent channel
assert bot.webhook_channel_id(topic) == 333
assert len(bot.bridges_for_group("12345678")) == 2
assert bot.outbound_conversation_id(topic) == "99887"

# one destination can only carry one bridge
try:
    bot.save_bridge("77777", 111, "", "other")
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError("channel 111 accepted a second bridge")

bot.set_last_seen("12345678", "900")
assert bot.last_seen("12345678") == "900"

bot.add_link(bot.LINK_GROUPME, "12345678", 111, "555", None, datetime.now(timezone.utc))
assert bot.link_exists(bot.LINK_GROUPME, "12345678", "555")
assert bot.link_exists(bot.LINK_GROUPME, "12345678", "555")
assert not bot.link_exists("nope", "12345678", "555")

# a Discord message can map to several GroupMe messages (text plus one per image)
bot.add_outbound("12345678", 2222, "555", datetime.now(timezone.utc))
bot.add_outbound("12345678", 2222, "556", datetime.now(timezone.utc))
assert sorted(row["groupme_id"] for row in bot.outbound_messages(2222)) == ["555", "556"]
assert bot.outbound_messages(3333) == []
assert bot.outbound_groupme_id_exists("12345678", "556")
assert not bot.outbound_groupme_id_exists("12345678", "999")
bot.remove_outbound("12345678", 2222, "555")
assert [row["groupme_id"] for row in bot.outbound_messages(2222)] == ["556"]
bot.remove_link(bot.LINK_GROUPME, "12345678", "555")
assert not bot.link_exists(bot.LINK_GROUPME, "12345678", "555")

# page-window helpers used to decide whether a missing message was really deleted
assert bot.oldest_numeric_id([{"id": "12"}, {"id": "10"}]) == "10"
assert bot.oldest_numeric_id([{"id": "abc"}]) == ""
assert bot.oldest_numeric_id([]) == ""
assert bot.message_id_at_least("12", "10")
assert not bot.message_id_at_least("9", "10")
assert not bot.message_id_at_least("x", "10")

# GroupMe exposes deletions as a tombstone and as a `message.deleted` event
assert bot.groupme_deleted_message_id({"id": "9", "deleted_at": 123}) == "9"
assert bot.groupme_deleted_message_id(
    {"id": "10", "event": {"type": "message.deleted", "data": {"message_id": "9"}}}
) == "9"
assert bot.groupme_deleted_message_id({"id": "9", "text": "This message was deleted"}) == ""
assert bot.groupme_deleted_message_id({"id": "9"}) == ""

# retargeting a bridge keeps the destination and the webhook
bot.update_bridge_conversation("99887", "99888", "topic", "dekalb", "user")
retargeted = bot.bridge_for_destination(222)
assert retargeted["conversation_id"] == "99888", retargeted["conversation_id"]
assert retargeted["conversation_name"] == "dekalb"
assert retargeted["parent_id"] == 333 and retargeted["target_kind"] == "thread"
assert bot.bridge_for_conversation("99887") is None

bot.set_bridge_bot("12345678", "botxyz")
assert bot.bridge_for_conversation("12345678")["bot_id"] == "botxyz"

# a main-chat bridge can keep its bot for callbacks but post with the access token
bot.set_bridge_send_mode("12345678", "user")
assert bot.bridge_for_conversation("12345678")["send_mode"] == "user"
assert bot.bridge_for_conversation("12345678")["bot_id"] == "botxyz"

# bridge-wide GroupMe display-name privacy, matched without case or extra whitespace
assert bot.groupme_user_mode("Sheng  Jiang") == ""
bot.set_groupme_user_mode("Sheng  Jiang", bot.PRIVACY_ANONYMIZE)
assert bot.groupme_user_mode("sheng jiang") == bot.PRIVACY_ANONYMIZE
assert [row["name"] for row in bot.list_groupme_user_modes()] == ["Sheng Jiang"]
bot.set_groupme_user_mode("Sheng Jiang", bot.PRIVACY_OPTOUT)
assert bot.groupme_user_mode("SHENG JIANG") == bot.PRIVACY_OPTOUT
assert len(bot.list_groupme_user_modes()) == 1
bot.set_groupme_user_mode("Sheng Jiang", bot.PRIVACY_SHARE)
assert bot.groupme_user_mode("Sheng Jiang") == ""
assert bot.list_groupme_user_modes() == []

# replies to an anonymized or opted-out display name do not leak it
bot.set_groupme_user_mode("Private Person", bot.PRIVACY_ANONYMIZE)
quote = bot.groupme_message_text(
    {
        "id": "1",
        "text": "reply",
        "attachments": [{"type": "reply", "name": "Private Person", "text": "hello"}],
    }
)
assert bot.ANONYMOUS_GROUPME_NAME in quote and "Private Person" not in quote, quote
bot.set_groupme_user_mode("Private Person", bot.PRIVACY_OPTOUT)
quote = bot.groupme_message_text(
    {
        "id": "2",
        "text": "reply",
        "attachments": [{"type": "reply", "name": "Private Person", "text": "secret"}],
    }
)
assert "secret" not in quote and "Private Person" not in quote, quote
bot.set_groupme_user_mode("Private Person", bot.PRIVACY_SHARE)

bot.delete_bridge("12345678")
assert bot.bridge_for_conversation("12345678") is None and bot.last_seen("12345678") is None
assert bot.outbound_messages(2222) == []
bot.delete_bridge("99888")

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
video_message = {
    "attachments": [{"type": "video", "url": "https://v.groupme.com/clip.mp4"}]
}
assert bot.groupme_video_urls(video_message) == ["https://v.groupme.com/clip.mp4"]
assert bot.groupme_message_text(video_message) == ""
assert bot.groupme_message_text({"attachments": [{"type": "video"}]}) == "[video]"
assert bot.groupme_message_text(
    {"attachments": [{"type": "video", "url": "not-a-url"}]}
) == "[video]"

# GroupMe replies render as a blockquote instead of a bare [reply] marker
reply = {
    "text": "sounds good",
    "attachments": [
        {"type": "reply", "reply_id": "1", "user_id": "2", "name": "Ann", "text": "meet at 7?"}
    ],
}
rendered_reply = bot.groupme_message_text(reply)
assert rendered_reply == "> Ann: meet at 7?\nsounds good", rendered_reply
assert bot.groupme_message_text({"attachments": [{"type": "reply", "name": "Ann"}]}) == "> Replying to Ann"
assert bot.groupme_message_text({"attachments": [{"type": "reply"}]}) == "> (GroupMe reply)"
assert "[reply]" not in bot.groupme_message_text({"text": "x", "attachments": [{"type": "reply"}]})
# a reply whose quoted text is known but whose author is not
assert bot.groupme_message_text({"attachments": [{"type": "reply", "text": "hi"}]}) == "> hi"

# GroupMe mentions become bold, backslash-escaped names, never a [mentions] token
mention_text = "@Leigh McDougal @John Snarey - hello"
mention_message = {
    "text": mention_text,
    "attachments": [
        {"type": "mentions", "loci": [[0, 15], [16, 12]], "user_ids": ["1", "2"]}
    ],
}
rendered_mentions = bot.groupme_message_text(mention_message)
assert "[mentions]" not in rendered_mentions, rendered_mentions
assert r"**\@Leigh McDougal**" in rendered_mentions, rendered_mentions
assert r"**\@John Snarey**" in rendered_mentions, rendered_mentions
assert "\u200b" not in rendered_mentions, rendered_mentions
# a mentions attachment with no text at all renders nothing instead of a marker
assert bot.groupme_message_text({"attachments": [{"type": "mentions"}]}) == ""
# an empty or out-of-range loci is left as plain text rather than raising
assert bot.groupme_message_text(
    {"text": "@Ann hi", "attachments": [{"type": "mentions", "loci": [[99, 4]]}]}
) == "@Ann hi"
# display-name privacy anonymizes mentioned names; opt-out is anonymized, not dropped
anonymous_mention = f"**\\@{bot.ANONYMOUS_GROUPME_NAME}**"
bot.set_groupme_user_mode("John Snarey", bot.PRIVACY_ANONYMIZE)
anonymized_mentions = bot.groupme_message_text(mention_message)
assert "John Snarey" not in anonymized_mentions, anonymized_mentions
assert anonymous_mention in anonymized_mentions, anonymized_mentions
assert r"**\@Leigh McDougal**" in anonymized_mentions, anonymized_mentions
bot.set_groupme_user_mode("John Snarey", bot.PRIVACY_OPTOUT)
opted_out_mentions = bot.groupme_message_text(mention_message)
assert "John Snarey" not in opted_out_mentions, opted_out_mentions
assert "hello" in opted_out_mentions, opted_out_mentions
assert anonymous_mention in opted_out_mentions, opted_out_mentions
assert r"**\@Leigh McDougal**" in opted_out_mentions, opted_out_mentions
bot.set_groupme_user_mode("John Snarey", bot.PRIVACY_SHARE)
assert bot.list_groupme_user_modes() == []

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

# a server nickname resolved for the relay wins over the name on the mention
class FakeUnresolvedMessage:
    id = 2
    content = "ping <@77> and <@!78>"
    mentions = []
    role_mentions = []
    channel_mentions = []


renamed = bot.render_discord_content(FakeMessage, {42: "Ann (nick)"})
assert renamed == "hey @Ann (nick) @Mods in #general done", renamed
# ids discord.py never resolved still get their nickname instead of `@user`
unresolved = bot.render_discord_content(FakeUnresolvedMessage, {77: "Cobb", 78: "Dekalb"})
assert unresolved == "ping @Cobb and @Dekalb", unresolved
assert bot.render_discord_content(FakeUnresolvedMessage) == "ping @user and @user"

# command tree wiring
names = sorted(c.name for c in bot.client.tree.get_commands())
assert names == ["bridge", "privacy"], names
print("smoke ok")
