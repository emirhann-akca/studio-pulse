#!/usr/bin/env python3
"""The cloud collector's keys, each write key bound to its apps (bootstrap.sh runs this).

    python3 deploy/cloudrun/make_keys.py keys <collector.env> [--plan] LABEL=app_id|app_id ...
    python3 deploy/cloudrun/make_keys.py sdk <collector.env> <dir>

`keys` gives every LABEL one public write key, bound to its app ids (bundle id /
package name: the SDK's ctx.app_id), and makes one read key for /live, in
<collector.env> (mode 600). PULSE_WRITE_KEYS there is `key=app_id|app_id,...`, the
form the collector reads: a bound key answers 403 for any other app, so a key pulled
out of one game cannot write events or revenue for another. Keys already in the file
are kept (games that ship them keep working) and bound to the apps given now; a key
the file holds without its apps is refused, because it would write for any app.

`sdk` writes <dir>/<label>.env for every key: PULSE_COLLECTOR_URL and that app's
PULSE_WRITE_KEY, what a release build needs, never the read key.

Keys are never printed: each is shown by its key id, the hash prefix the collector
stores with every row. Standard library only (bootstrap runs it with python3).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

# contract/pulse_v2.yaml ctx.app_id (a test keeps the two equal)
APP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,149}$")
LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
KEY_VAR, APPS_VAR = "PULSE_WRITE_KEY_", "PULSE_WRITE_APPS_"
OURS = re.compile(r"^(PULSE_WRITE_KEYS|PULSE_READ_KEY|PULSE_WRITE_KEY_\w+|PULSE_WRITE_APPS_\w+)$")


class Problem(ValueError):
    pass


def key_id(key: str) -> str:
    """The collector's label for a key (collector/settings.py key_id)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def parse_apps(specs: list[str]) -> dict[str, list[str]]:
    """`label=app_id|app_id` entries -> {label: [app_id, ...]}; unbound entries are refused."""
    out: dict[str, list[str]] = {}
    for spec in specs:
        label, sep, rest = spec.partition("=")
        label = label.strip().lower()
        apps = [a.strip() for a in rest.split("|") if a.strip()]
        if not LABEL.match(label):
            raise Problem(
                f"{spec!r}: a label is letters, digits and _ (e.g. puzzle=com.example.puzzle)"
            )
        if not sep or not apps:
            raise Problem(
                f"{label}: give its app ids as {label}=app_id|app_id "
                "(a key without apps would write for any app)"
            )
        bad = [a for a in apps if not APP_ID.match(a)]
        if bad:
            raise Problem(f"{label}: {bad[0]!r} is not an app id (bundle id / package name)")
        if label in out:
            raise Problem(f"{label} is listed twice")
        out[label] = list(dict.fromkeys(apps))
    if not out:
        raise Problem("name at least one app: LABEL=app_id|app_id")
    return out


def read_env(path: Path) -> tuple[dict[str, str], list[str]]:
    """NAME=VALUE pairs of the file, and its other lines (kept as they are)."""
    values: dict[str, str] = {}
    other: list[str] = []
    if not path.is_file():
        return values, other
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and OURS.match(name.strip()):
            values[name.strip()] = value.strip()
        elif line.strip():
            other.append(line)
    return values, other


def plan(
    values: dict[str, str], wanted: dict[str, list[str]], where: str
) -> dict[str, tuple[str, list[str], bool]]:
    """{label: (key, apps, is_new)}: the wanted labels first, then labels only the file has."""
    labels: dict[str, tuple[str, list[str], bool]] = {}
    for label, apps in wanted.items():
        old = values.get(KEY_VAR + label.upper())
        labels[label] = (old or f"pk_{label}_{secrets.token_hex(12)}", apps, not old)
    for name, key in values.items():
        if not name.startswith(KEY_VAR):
            continue
        label = name[len(KEY_VAR) :].lower()
        if label in labels:
            continue
        apps = [a for a in values.get(APPS_VAR + label.upper(), "").split("|") if a]
        if not apps:
            raise Problem(
                f"{name} in {where} has no apps: add {label}=app_id|app_id to APPS "
                "(or delete its line); an unbound key writes for any app"
            )
        labels[label] = (key, apps, False)
    named = {k for k, _, _ in labels.values()}
    for item in filter(None, values.get("PULSE_WRITE_KEYS", "").split(",")):
        if item.partition("=")[0] not in named:
            raise Problem(
                f"PULSE_WRITE_KEYS in {where} holds a key no {KEY_VAR}<LABEL> line names; "
                "give that key a label line or move the file aside to make new keys"
            )
    return labels


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, readable by the owner only, never half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_keys(path: Path, specs: list[str], *, dry: bool) -> int:
    values, other = read_env(path)
    labels = plan(values, parse_apps(specs), str(path))
    read_key = values.get("PULSE_READ_KEY") or secrets.token_urlsafe(32)
    lines = []
    for label, (key, apps, _) in labels.items():
        lines += [f"{KEY_VAR}{label.upper()}={key}", f"{APPS_VAR}{label.upper()}={'|'.join(apps)}"]
    lines.append(
        "PULSE_WRITE_KEYS=" + ",".join(f"{k}={'|'.join(a)}" for k, a, _ in labels.values())
    )
    lines.append(f"PULSE_READ_KEY={read_key}")
    for label, (key, apps, new) in labels.items():
        state = "new" if new else "kept"
        print(f"  {label}: write key {key_id(key)} ({state}) bound to {', '.join(apps)}")
    print(f"  read key: {'kept' if values.get('PULSE_READ_KEY') else 'new'}")
    if dry:
        print(f"  (plan: {path} not written)")
        return 0
    write_private(path, "\n".join(lines + other) + "\n")
    return 0


def cmd_sdk(path: Path, out: Path) -> int:
    values, other = read_env(path)
    url = next(
        (ln.partition("=")[2].strip() for ln in other if ln.startswith("PULSE_COLLECTOR_URL=")), ""
    )
    if not url:
        raise Problem(f"no PULSE_COLLECTOR_URL in {path} yet (deploy first)")
    labels = plan(values, {}, str(path))
    if not labels:
        raise Problem(f"no write keys in {path}")
    for label, (key, apps, _) in labels.items():
        write_private(
            out / f"{label}.env",
            f"# {label}: public, write-only collector key, accepted for {', '.join(apps)} only\n"
            f"PULSE_COLLECTOR_URL={url}\nPULSE_WRITE_KEY={key}\n",
        )
        print(f"  {out / (label + '.env')}: key {key_id(key)} for {', '.join(apps)}")
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) >= 2 and argv[0] == "keys":
            rest = argv[2:]
            dry = "--plan" in rest
            return cmd_keys(Path(argv[1]), [a for a in rest if a != "--plan"], dry=dry)
        if len(argv) == 3 and argv[0] == "sdk":
            return cmd_sdk(Path(argv[1]), Path(argv[2]))
    except Problem as exc:
        print(f"make_keys: {exc}", file=sys.stderr)
        return 2
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
