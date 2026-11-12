"""robots.txt per RFC 9309 (the rules Google itself applies).

urllib.robotparser uses first-match prefix rules and treats `*` inside a path literally, so
`Disallow: /store/apps/datasafety*` would not match and a later `Allow` could be ignored. This
parser implements the standard: the group for our product token (else `*`), the longest
matching rule wins, an Allow wins a tie, `*` matches any run of characters and `$` anchors the
end. Paths are matched with their query string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str] = field(repr=False)

    @classmethod
    def make(cls, allow: bool, pattern: str) -> Rule:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in _norm(body))
        return cls(allow, pattern, re.compile(rx + ("$" if anchored else ""), re.S))


def _norm(path: str) -> str:
    # compare decoded paths (RFC 9309 2.2.2: percent-encoded and plain characters are equivalent)
    return unquote(path)


@dataclass
class Robots:
    groups: list[tuple[list[str], list[Rule]]]

    @classmethod
    def parse(cls, text: str) -> Robots:
        groups: list[tuple[list[str], list[Rule]]] = []
        agents: list[str] = []
        rules: list[Rule] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if value:  # an empty Disallow allows everything
                    rules.append(Rule.make(key == "allow", value))
            else:
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))
        return cls(groups)

    def rules_for(self, product: str) -> list[Rule]:
        product = product.lower()
        mine = [r for agents, rules in self.groups if product in agents for r in rules]
        if mine:
            return mine
        return [r for agents, rules in self.groups if "*" in agents for r in rules]

    def allowed(self, url_or_path: str, product: str) -> bool:
        parts = urlsplit(url_or_path)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        target = _norm(path)
        best: Rule | None = None
        for r in self.rules_for(product):
            m = r.regex.match(target)
            if not m:
                continue
            length = len(r.pattern)
            if (
                best is None
                or length > len(best.pattern)
                or (length == len(best.pattern) and r.allow and not best.allow)
            ):
                best = r
        return best is None or best.allow
