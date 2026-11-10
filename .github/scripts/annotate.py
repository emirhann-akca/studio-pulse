"""Run a command in CI; if it fails, publish its last lines as an error annotation.

Job logs of a public repository need a token to read, annotations do not, so
every step that can fail goes through this wrapper:

    python .github/scripts/annotate.py "<title>" <command> [args ...]
"""

from __future__ import annotations

import collections
import subprocess
import sys


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_prop(text: str) -> str:
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def main() -> int:
    title, cmd = sys.argv[1], sys.argv[2:]
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    tail: collections.deque[str] = collections.deque(maxlen=30)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        print(f"::error title={escape_prop(title)}::cannot start {cmd[0]}: {escape(str(exc))}")
        return 127
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace")
        sys.stdout.write(line)
        tail.append(line.rstrip())
    rc = proc.wait()
    sys.stdout.flush()
    if rc:
        body = escape("\n".join(tail))[:60000]
        print(f"::error title={escape_prop(title)} (exit {rc})::{body}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
