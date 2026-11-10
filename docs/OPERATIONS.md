# Operating studio-pulse

How a refresh runs unattended on a laptop, how failures surface, and what is
different on Windows. Commands are the same on every OS.

## Install

`install.sh` (macOS / Linux) and `install.ps1` (Windows PowerShell 5.1 or 7)
install uv 0.13.7 if it is missing, then `uv tool install` the studio-pulse
wheel with Python 3.13 (downloaded by uv when absent), and run `pulse doctor`.
No administrator rights; nothing is installed outside your user profile.

| Variable | Meaning |
|---|---|
| `PULSE_SOURCE` | wheel path or URL (default: the release wheel of the installer's version) |
| `PULSE_EXTRAS` | extras, default `gcp` (BigQuery / Cloud Storage clients); `none` for a smaller install |
| `PULSE_PYTHON` | Python version for the tool, default `3.13` |
| `PULSE_DEMO=1` | also build the demo into `PULSE_DEMO_DIR` (default `~/studio-pulse-demo`) |
| `UV_NO_MODIFY_PATH=1` | do not touch shell profiles / the user PATH |

Upgrade by running the installer of the newer version; uninstall with
`uv tool uninstall studio-pulse` (after `pulse schedule remove`).

On Windows, `irm ... | iex` works under any execution policy because no script
file is run; the installer starts uv's own installer with `-ExecutionPolicy
Bypass` for that process only. Executables land in `%USERPROFILE%\.local\bin`;
open a new terminal after the first install so it is on PATH.

## Files of one deployment

Everything is relative to the config file unless the config says otherwise:

| Path | Content |
|---|---|
| `warehouse` (default `local/pulse.duckdb`) | the DuckDB warehouse |
| `site_dir` (default `local/site`) | the static dashboard |
| `state_dir` (default `<warehouse dir>/pulse-state`) | `run.lock`, `heartbeat.json`, `runs.jsonl` (last 200 runs), `snapshot.duckdb`, `logs/pulse.log` (rotated at 5 MB) |
| `checks` / `alerts` | rule files; default `./checks.yaml`, `./alerts.yaml`, then the copies shipped in the package |
| user state (`~/Library/Application Support/studio-pulse`, `%LOCALAPPDATA%\studio-pulse`) | one record per registered schedule (`schedules/<name>.json`) |

## A refresh

`pulse run` takes the run lock, then ingests, builds the models, runs the
checks, evaluates the alerts, sends notifications (with `--notify`) and writes
the dashboard. The lock is a file created with `O_CREAT | O_EXCL`, which behaves
the same on Windows and POSIX; a lock whose process is gone, or that is older
than six hours, is broken automatically.

Exit codes: `0` success; `1` a source failed to load or an error-level check
failed (the dashboard is still written, so the failure is visible); `2`
configuration error; `75` another refresh holds the lock.

`heartbeat.json` records the last start, last end, duration, last success, last
failure and error. A heartbeat that still says `running` while its process is
gone is reported as `interrupted` (power loss, killed process).

## The daily schedule

```
pulse schedule install --config pulse.local.yaml --at 09:30     # --dry-run prints the definition
pulse schedule status                                           # exit 3 when not registered
pulse schedule run-now --wait 600                               # start it, wait for the heartbeat
pulse schedule remove
```

`--name` (default `refresh`) allows several jobs, e.g. one per config.

- **macOS**: `~/Library/LaunchAgents/dev.studio-pulse.<name>.plist`, loaded with
  `launchctl bootstrap gui/<uid>` and removed with `launchctl bootout` (the
  non-legacy subcommands). `StartCalendarInterval` starts a run that was missed
  while the Mac slept as soon as it wakes. Sessions without a GUI login (ssh)
  get the job in the `user/<uid>` domain instead.
- **Windows**: task `\StudioPulse\<name>`, imported from XML with
  `schtasks /create /xml` because schtasks' flags cannot set the laptop
  settings: start on battery, keep running when unplugged, start a missed run
  as soon as possible, never run two at once, stop after two hours. The task
  runs `pulse-bg.exe`, a windowless launcher, so no console window appears, and
  runs with your normal rights while you are logged in.
- Both run `pulse-bg --log-file <state>/logs/pulse.log run --config <config>`,
  so the scheduler's last exit code is the refresh's exit code.

Google credentials: a scheduled run uses the same Application Default
Credentials as your terminal (`gcloud auth application-default login`), stored
in `~/.config/gcloud` or `%APPDATA%\gcloud`.

## Data-quality checks

`checks.yaml` defines each check as one SQL query that returns failing rows
(types and options are documented at the top of `src/studio_pulse/checks.py`).
`severity: error` fails `pulse check` (exit 1) and `pulse run`; `warn` is
reported and notified but does not fail. Results are kept in the warehouse
table `meta_check_results` (last 30 runs), shown on the dashboard and in the UI.

Freshness compares each configured source's newest data date with the as-of
date. Sources that only have rows when something happens (Crashlytics: no
crash, no row; RevenueCat; App Store sales) are listed under `sparse` and judged
by their last successful load instead, so a quiet week is not reported as a
stale source.

## The local UI

`pulse serve --config <config>` serves http://127.0.0.1:8765 (`--port 0` picks a
free port, `--open` opens the browser, `--check` requests every page once and
exits, which CI uses on every OS).

- DuckDB allows one writing process, and a process that holds the file open,
  even read-only, blocks that writer. The UI therefore opens the warehouse
  read-only for each request and closes it straight away, and a refresh waits
  up to 60 s for such readers. While a refresh is running, or the file is
  locked, pages come from `snapshot.duckdb`, the copy taken after the last
  successful run, and say so.
- It answers only requests addressed to this machine (Host header check, against
  DNS rebinding). "Run refresh now" needs a token that only pages served by
  this process carry, so another web page cannot trigger a run.
- `--host 0.0.0.0` exposes it to your network; there is no login.

Ad-hoc SQL: the DuckDB UI extension also works from the Python package
(checked with duckdb 2.0.0 on macOS; it downloads the extension, about 44 MB,
and loads its web assets from ui.duckdb.org). Point it at the snapshot so it
never blocks a refresh; it needs the folder `~/.duckdb` to exist:

```
python -c "import duckdb; c = duckdb.connect('local/pulse-state/snapshot.duckdb', read_only=True); c.sql('CALL start_ui()'); input('Enter stops the UI')"
```

`duckdb -ui` is the same thing from the separate DuckDB command-line tool, which
studio-pulse does not install.

## Windows notes

- Time zones: Windows has no IANA database; the `tzdata` package (installed on
  Windows automatically) provides it. `pulse doctor` checks it.
- Encodings: every file is read and written as UTF-8, whatever the console
  code page; console output that the code page cannot show is escaped instead
  of crashing. Paths with non-ASCII characters (`C:\Users\Şule\...`) are tested
  in CI.
- Paths: without `LongPathsEnabled`, Windows limits paths to 260 characters;
  keep the data folder short (`pulse doctor` reports the setting). Avoid
  OneDrive-synced folders for the warehouse: sync clients lock open files.
- Line endings: `.gitattributes` keeps every text file LF in a Windows
  checkout, so models and fixtures are byte-identical to the other platforms.
