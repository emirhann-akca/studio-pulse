"""`pulse-bg`: run a pulse command with its output in a log file.

Installed as a GUI script, so on Windows it is a windowless executable: a
scheduled refresh does not flash a console window, and Task Scheduler records
the exit code as the task's last result. On macOS and Linux it behaves like
`pulse` with redirected output.

    pulse-bg [--log-file PATH] <pulse arguments ...>

Without --log-file the log goes to $PULSE_LOG_FILE or the per-user state
directory. The file rotates at 5 MB (3 old copies are kept).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import traceback
from pathlib import Path

ROTATE_BYTES = 5 * 1024 * 1024
KEEP = 3


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < ROTATE_BYTES:
            return
    except FileNotFoundError:
        return
    for i in range(KEEP - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def main(argv: list[str] | None = None) -> int:
    from .schedule import user_state_dir

    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--log-file"] and len(args) >= 2:
        log_file = Path(args[1])
        args = args[2:]
    elif os.environ.get("PULSE_LOG_FILE"):
        log_file = Path(os.environ["PULSE_LOG_FILE"])
    else:
        log_file = user_state_dir() / "logs" / "pulse-bg.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _rotate(log_file)
    old_out, old_err = sys.stdout, sys.stderr
    with open(log_file, "a", encoding="utf-8", errors="backslashreplace", buffering=1) as log:
        sys.stdout = sys.stderr = log
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"=== {stamp} pulse {' '.join(args)} (pid {os.getpid()})")
        try:
            from .cli import main as cli_main

            rc = cli_main(args)
        except SystemExit as exc:  # argparse errors
            rc = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
            rc = 1
        print(f"=== exit {rc}")
        sys.stdout, sys.stderr = old_out, old_err
    return int(rc or 0)


def gui_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    gui_main()
