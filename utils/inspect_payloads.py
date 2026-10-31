"""Summarize captured GroupMe callback payloads (LOG_GROUPME_PAYLOADS=true).

Shows the keys of each payload plus anything that looks topic-related, which is how
the subgroup (topic) fields were discovered.

    python utils/inspect_payloads.py [--path groupme_payloads.jsonl] [--limit 10]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "groupme_payloads.jsonl"
TOPIC_HINTS = ("topic", "thread", "sub", "conversation", "parent")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize GroupMe callback payloads")
    parser.add_argument("--path", default=str(DEFAULT_LOG), help="payload log to read")
    parser.add_argument("--limit", type=int, default=20, help="how many payloads to show")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no payload log at {path}")
        return 1

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"{len(rows)} payload(s) in {path}")

    for row in rows[-args.limit :]:
        payload = row.get("payload") or {}
        keys = ", ".join(sorted(payload))
        interesting = {
            key: value
            for key, value in payload.items()
            if any(hint in key.lower() for hint in TOPIC_HINTS)
        }
        print(f"\n- {row.get('logged_at')}")
        print(f"  keys: {keys}")
        print(f"  topic-ish: {interesting or 'none'}")
        print(
            "  id={0} sender_type={1} system={2} group_id={3}".format(
                payload.get("id"),
                payload.get("sender_type"),
                payload.get("system"),
                payload.get("group_id"),
            )
        )
        print(f"  text: {(payload.get('text') or '')[:80]!r}")
        print(f"  attachments: {[a.get('type') for a in payload.get('attachments') or []]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
