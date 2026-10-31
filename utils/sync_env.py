"""Keep .env in sync with .env.example without losing any values.

Adds keys that only exist in the example, keeps every value already present, and
carries over keys that were removed from the example. The previous file is backed
up to a temp directory before it is rewritten.

    python utils/sync_env.py
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
KEY_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")


def parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = KEY_RE.match(line.strip())
            if match:
                values[match.group(1)] = match.group(2)
    return values


def main() -> int:
    if not EXAMPLE.exists():
        print(f"missing {EXAMPLE}")
        return 1
    if not ENV.exists():
        shutil.copy2(EXAMPLE, ENV)
        print(f"created {ENV.name} from {EXAMPLE.name}")
        return 0

    current = parse(ENV)
    template = parse(EXAMPLE)
    example_lines = EXAMPLE.read_text(encoding="utf-8").splitlines()

    merged: list[str] = []
    seen: set[str] = set()
    for line in example_lines:
        match = KEY_RE.match(line)
        if match:
            key, default = match.group(1), match.group(2)
            seen.add(key)
            merged.append(f"{key}={current.get(key, default)}")
        else:
            merged.append(line)

    extras = [key for key in current if key not in seen]
    if extras:
        merged.append("")
        merged.append("# Keys kept from the previous .env (not in .env.example)")
        merged.extend(f"{key}={current[key]}" for key in extras)

    backup = Path(tempfile.mkdtemp()) / ".env.backup"
    shutil.copy2(ENV, backup)
    ENV.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8", newline="\n")

    print(f"backup: {backup}")
    print("added:", ", ".join(k for k in template if k not in current) or "(none)")
    print("preserved:", sum(1 for key in template if key in current))
    print("carried over:", ", ".join(extras) or "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
