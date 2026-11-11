"""Print an install one-liner exactly as README.md shows it.

    python readme_oneliner.py sh|ps [git-ref]

With a ref, only the branch segment of the raw.githubusercontent.com URL is
replaced (main -> the commit under test), so CI runs the README's own text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "sh": r"curl -LsSf https://raw\.githubusercontent\.com/[\w.-]+/[\w.-]+/main/install\.sh \| sh",
    "ps": r'powershell -ExecutionPolicy ByPass -c "irm https://raw\.githubusercontent\.com/'
    r'[\w.-]+/[\w.-]+/main/install\.ps1 \| iex"',
}


def oneliner(kind: str, ref: str | None = None, readme: Path = Path("README.md")) -> str:
    text = readme.read_text(encoding="utf-8")
    found = [ln.strip() for ln in text.splitlines() if re.fullmatch(PATTERNS[kind], ln.strip())]
    if len(found) != 1:
        raise SystemExit(f"README.md: expected one {kind} install line, found {len(found)}")
    line = found[0]
    if ref:
        line = re.sub(r"(raw\.githubusercontent\.com/[\w.-]+/[\w.-]+)/main/", rf"\1/{ref}/", line)
    return line


if __name__ == "__main__":
    print(oneliner(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
