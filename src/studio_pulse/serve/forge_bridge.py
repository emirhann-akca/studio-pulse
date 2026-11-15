"""The ad centre's line to creative-forge: its `forge` command, run as a subprocess with JSON output.

Why a subprocess and not creative-forge's local HTTP app:

* creative-forge's pages carry a per-process CSRF token that only its own pages can read, by design; borrowing it
  from another program would weaken exactly the protection it exists for. The CLI needs no token.
* The CLI works whether or not the creative-forge app is open, and every write goes through the same engine code,
  with the same gates, as when a person types the command: review decisions bound to the file's SHA-256, publishing
  only approved and QA-clean exports, campaigns created PAUSED, activation only with the typed phrase and under the
  caps, and nothing on a live account without the explicit live switch.
* Arguments are a list (no shell), every call has a timeout, output is versioned JSON (`schema` keys such as
  `creative-forge/snapshot/v1`), and a missing or broken `forge` only turns the create / publish half of the page
  off: Studio Pulse keeps showing its own data, read-only.

Which `forge`: `reklam.forge` in the config, else $PULSE_FORGE_BIN, else `forge` on PATH, else ~/.local/bin/forge
(where creative-forge's installer puts it). `reklam.forge_root`, `forge_state` and `forge_db` (relative to the
config) point it at a runs folder, campaign state and performance store; the demo config sets all three so trying
the flow there never writes into a real creative-forge data folder. Without them creative-forge uses its own
defaults (the desktop app's runs folder and the user data folder), so both apps see the same work.

Long jobs (`forge make`) run detached with `--progress FILE`; the page polls that file. Nothing here ever passes
`--live` unless the person ticked the live box on a campaign that is in live mode, the same rule as creative-forge's
own Kampanya page.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "creative-forge/snapshot/v1"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
MEDIA_SUFFIXES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
API_NETWORKS = ("meta", "tiktok", "google")
MANUAL_NETWORKS = ("applovin", "unity", "mintegral")
PLAYABLE_NETWORKS = ("applovin", "unity", "meta", "google", "tiktok", "mintegral")


class ForgeError(RuntimeError):
    """A forge command failed or answered something unreadable; the message is safe to show."""


@dataclass
class Result:
    code: int
    data: Any
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0 and self.data is not None


@dataclass
class Job:
    id: str
    app: str
    kind: str
    progress: Path
    proc: subprocess.Popen[bytes] | None
    started: float
    out: Path
    errf: Path
    args: dict[str, Any] = field(default_factory=dict)

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            data = json.loads(self.progress.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        rc = self.proc.poll() if self.proc is not None else None
        result = None
        if rc is not None:
            try:
                result = json.loads(self.out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = None
        state = data.get("state") or "running"
        if rc is not None and state == "running":
            state = "done" if rc == 0 or (result and result.get("exports")) else "failed"
        if rc not in (None, 0) and not result:
            state = "failed"
        err = ""
        if state == "failed":
            try:
                err = _tail(self.errf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                err = ""
        return {
            "id": self.id,
            "app": self.app,
            "kind": self.kind,
            "state": state,
            "done": int(data.get("done") or 0),
            "total": int(data.get("total") or 1),
            "message": str(data.get("message") or ""),
            "seconds": round(time.time() - self.started),
            "result": result or data.get("result"),
            "error": err or (data.get("message") if state == "failed" else ""),
            "args": self.args,
        }


def _tail(text: str, n: int = 4) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])[-600:]


def locate(explicit: str | None) -> Path | None:
    for cand in (explicit, os.environ.get("PULSE_FORGE_BIN")):
        if cand:
            p = Path(cand).expanduser()
            return p if p.is_file() else None
    found = shutil.which("forge")
    if found:
        return Path(found)
    home = Path.home() / ".local" / "bin" / ("forge.exe" if sys.platform == "win32" else "forge")
    return home if home.is_file() else None


class Forge:
    def __init__(self, cfg: Any, *, binary: Path | None | str = "auto") -> None:
        rk = (getattr(cfg, "raw", None) or {}).get("reklam") or {}
        base = Path(getattr(cfg, "base_dir", None) or ".")

        def path(key: str) -> Path | None:
            v = rk.get(key)
            if not v:
                return None
            p = Path(str(v)).expanduser()
            return p if p.is_absolute() else (base / p).resolve()

        self.bin = (
            locate(rk.get("forge")) if binary == "auto" else (Path(binary) if binary else None)
        )
        self.root = path("forge_root")
        self.state_dir = path("forge_state")
        self.db = path("forge_db")
        state = Path(getattr(cfg, "state_dir", None) or base / "pulse-state")
        self.work = state / "reklam"
        self.timeout = float(rk.get("forge_timeout_s", 120))
        self._lock = threading.Lock()
        self._snap: tuple[float, dict[str, Any] | None, str] = (0.0, None, "")
        self.jobs: dict[str, Job] = {}
        self.media: dict[
            str, Path
        ] = {}  # token -> file: only paths a snapshot listed are ever served

    # ---------------------------------------------------------------------------------------------- plumbing
    @property
    def available(self) -> bool:
        return self.bin is not None

    def _common(self, *, root: bool = False, state: bool = False, db: bool = False) -> list[str]:
        out = []
        if root and self.root:
            out += ["--root", str(self.root)]
        if state and self.state_dir:
            out += ["--state", str(self.state_dir)]
        if db and self.db:
            out += ["--db", str(self.db)]
        return out

    def argv(self) -> list[str]:
        """The command: the `forge` executable, or a .py file run by this Python (tests, a source checkout)."""
        assert self.bin is not None
        return [sys.executable, str(self.bin)] if self.bin.suffix == ".py" else [str(self.bin)]

    def run(self, args: list[str], *, timeout: float | None = None) -> Result:
        if self.bin is None:
            raise ForgeError("creative-forge is not installed")
        try:
            p = subprocess.run(  # noqa: S603  # nosec B603: a list of arguments, no shell
                [*self.argv(), *args],
                capture_output=True,
                timeout=timeout or self.timeout,
                cwd=str(self.work.parent if self.work.parent.is_dir() else Path.cwd()),
                env=_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ForgeError(
                f"forge {args[0]} did not answer within {timeout or self.timeout:.0f} s"
            ) from None
        except OSError as e:
            raise ForgeError(f"forge could not start ({type(e).__name__})") from None
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
        data = None
        if out.strip().startswith(("{", "[")):
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        return Result(p.returncode, data, _tail(err))

    # ---------------------------------------------------------------------------------------------- reading
    def snapshot(
        self, *, max_age: float = 30.0, force: bool = False
    ) -> tuple[dict[str, Any] | None, str]:
        """(snapshot, problem). Cached for `max_age` seconds; a failed read keeps the last good one."""
        if self.bin is None:
            return None, "missing"
        with self._lock:
            at, snap, problem = self._snap
            if not force and snap is not None and time.monotonic() - at < max_age:
                return snap, problem
        try:
            res = self.run(["snapshot", "--json", *self._common(root=True, state=True)], timeout=60)
            if (
                not res.ok
                or not isinstance(res.data, dict)
                or res.data.get("schema") != SNAPSHOT_SCHEMA
            ):
                raise ForgeError(res.err or "unexpected answer from forge snapshot")
            snap, problem = res.data, ""
        except ForgeError as e:
            problem = str(e)
            snap = self._snap[1]
        with self._lock:
            self._snap = (time.monotonic(), snap, problem)
            if snap:
                self._index(snap)
        return snap, problem

    def invalidate(self) -> None:
        with self._lock:
            self._snap = (0.0, self._snap[1], self._snap[2])

    def _index(self, snap: dict[str, Any]) -> None:
        for c in snap.get("creatives") or []:
            for key in ("path", "poster"):
                if c.get(key):
                    c[key + "_token"] = self.token(Path(c[key]))
        for p in snap.get("playables") or []:
            p["token"] = self.token(Path(p["html"]), allow=(".html",))
            for b in p.get("builds") or []:
                bp = Path(b["path"])
                b["token"] = self.token(
                    bp, allow=(bp.suffix.lower(),) if bp.is_dir() else (".html", ".zip")
                )

    def token(self, path: Path, allow: tuple[str, ...] | None = None) -> str | None:
        suffix = path.suffix.lower()
        if allow is None and suffix not in MEDIA_SUFFIXES:
            return None
        if allow is not None and suffix not in allow:
            return None
        tok = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        self.media[tok] = path
        return tok

    def media_path(self, token: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{24}", token or ""):
            return None
        p = self.media.get(token)
        return p if p is not None and p.exists() else None

    # ---------------------------------------------------------------------------------------------- writing
    def start_make(self, *, app: str, app_name: str, store_url: str | None, brief: str, variants: int,
                   languages: list[str], mechanic: str, providers: str, budget: float | None,
                   gameplay: str | None, hook: str | None = None, ref: str | None = None) -> Job:  # fmt: skip
        if self.bin is None:
            raise ForgeError("creative-forge is not installed")
        jid = secrets.token_hex(6)
        folder = self.work / "jobs"
        folder.mkdir(parents=True, exist_ok=True)
        progress, out, errf = (
            folder / f"{jid}.progress.json",
            folder / f"{jid}.out.json",
            folder / f"{jid}.err.txt",
        )
        args = ["make", "--json", "--app", app, "--app-name", app_name, "--variants", str(variants),
                "--providers", providers, "--progress", str(progress), "--mechanic", mechanic]  # fmt: skip
        if brief:
            args += ["--brief", brief]
        if store_url:
            args += ["--store-url", store_url]
        if hook:
            args += ["--hook", hook]
        if ref:
            args += ["--ref", ref]
        for lang in languages:
            args += ["--language", lang]
        if providers == "vertex":
            args += ["--budget-usd", f"{budget:.2f}"] if budget else []
            args += ["--gameplay", gameplay] if gameplay else []
        args += self._common(root=True)
        with open(out, "wb") as fo, open(errf, "wb") as fe:
            kwargs: dict[str, Any] = {
                "stdout": fo,
                "stderr": fe,
                "stdin": subprocess.DEVNULL,
                "env": _env(),
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.Popen([*self.argv(), *args], **kwargs)  # noqa: S603  # nosec B603
        job = Job(jid, app, "make", progress, proc, time.time(), out, errf,
                  {"variants": variants, "providers": providers, "brief": brief[:120]})  # fmt: skip
        with self._lock:
            self.jobs[jid] = job
        return job

    def job(self, jid: str) -> Job | None:
        return self.jobs.get(jid) if re.fullmatch(r"[0-9a-f]{12}", jid or "") else None

    def review(self, run_dir: str, file: str, decision: str, note: str, reviewer: str) -> Result:
        flag = "--approve" if decision == "approve" else "--reject"
        res = self.run(
            ["review", run_dir, flag, file, "--note", note, "--reviewer", reviewer, "--json"]
        )
        self.invalidate()
        return res

    def publish(self, run_dir: str, dest: Path) -> Result:
        res = self.run(["publish", run_dir, "--to", str(dest), "--json"])
        self.invalidate()
        return res

    def init_campaign(self, *, root: Path, name: str, creatives: str, store_url: str, daily: float, total: float,
                      countries: list[str], networks: list[str], app: str) -> Result:  # fmt: skip
        """A starter config under <root>/campaigns (mock accounts for the API networks, manual packs otherwise)."""
        args = ["campaign", "init", "--json", "--root", str(root), "--name", name, "--creatives", creatives,
                "--store-url", store_url, "--daily", f"{daily:.2f}", "--total-cap", f"{total:.2f}", "--app", app]  # fmt: skip
        for c in countries:
            args += ["--country", c]
        for n in networks:
            args += ["--network", n]
        return self.run(args)

    def create(self, config: str) -> Result:
        res = self.run(
            ["campaign", "create", config, "--json", *self._common(state=True, db=True)],
            timeout=300,
        )
        self.invalidate()
        return res

    def activate(self, ref: str, phrase: str, *, live: bool, config: str | None = None) -> Result:
        args = [
            "campaign",
            "activate",
            ref,
            "--confirm",
            phrase,
            "--json",
            *self._common(state=True, db=True),
        ]
        args += ["--config", config] if config else []
        args += ["--live"] if live else []
        res = self.run(args, timeout=180)
        self.invalidate()
        return res

    def pause(self, ref: str) -> Result:
        res = self.run(["campaign", "pause", ref, "--reason", "paused from studio-pulse",
                        "--json", *self._common(state=True, db=True)], timeout=180)  # fmt: skip
        self.invalidate()
        return res

    def playable_pack(self, source: str, network: str, root: str) -> Result:
        out = str(Path(root) / "playables" / "packed" / network)
        res = self.run(
            [
                "playable",
                "pack",
                source,
                "--network",
                network,
                "--cta-adapter",
                "--out",
                out,
                "--json",
            ]
        )
        self.invalidate()
        return res


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUTF8"] = "1"
    return env
