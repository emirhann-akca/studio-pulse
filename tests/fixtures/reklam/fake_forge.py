"""A stand-in for creative-forge's `forge` CLI in the ad-centre tests: canned JSON in the documented shapes.

Every call is appended to $FAKE_FORGE_LOG (one JSON list of arguments per line), so a test can check exactly what
studio-pulse asked for. State that changes (review decisions, campaign status) lives in $FAKE_FORGE_STATE.
Nothing here touches a network; it is not creative-forge, only its contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STORE = "https://play.google.com/store/apps/details?id=demo.mossymatch"
STATE = Path(os.environ["FAKE_FORGE_STATE"])
ROOT = Path(os.environ["FAKE_FORGE_ROOT"])


def load() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {
        "review": "pending",
        "status": "PAUSED",
        "mode": os.environ.get("FAKE_FORGE_MODE", "mock"),
    }


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s), encoding="utf-8")


def out(obj: object) -> None:
    sys.stdout.write(json.dumps(obj))


def arg(args: list[str], name: str) -> str | None:
    """`--name value` or `--name=value` (studio-pulse sends the second form)."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1 :]
    return None


def main(args: list[str]) -> int:
    with open(os.environ["FAKE_FORGE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
    s = load()
    run = ROOT / "runs" / "mossy-match-1"
    video = run / "cf1.mm.brief.clean.main.main.9x16.tr.0123456789.mp4"
    if args[0] == "snapshot":
        run.mkdir(parents=True, exist_ok=True)
        if not video.exists():  # written once: on Windows a file being served cannot be rewritten
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        app = {
            "id": "mossy_match",
            "name": "Mossy Match",
            "store_url": STORE,
            "store_id": "demo.mossymatch",
        }
        pl = ROOT / "playables" / "src" / "mm-sample"
        pl.mkdir(parents=True, exist_ok=True)
        if not (pl / "index.html").exists():
            (pl / "index.html").write_text("<!doctype html><title>p</title><script>document.title='x'</script>",
                                           encoding="utf-8")  # fmt: skip
        out({
            "schema": "creative-forge/snapshot/v1", "version": "0.0.0", "root": str(ROOT),
            "creatives": [{
                "key": "runs/mossy-match-1/" + video.name, "rid": "runs/mossy-match-1", "file": video.name,
                "path": str(video), "poster": None, "creative_id": "4a69bf2c4d",
                "ad_name": "cf1.mossy-ads.satisfying-clean.main.main.main.9x16.en.4a69bf2c4d", "format": "9:16",
                "variant": "clean · tr", "qa_status": "pass", "failed": [], "warned": [], "review": s["review"],
                "decision": None, "hypothesis": "dust <b>bold</b>", "app": app, "modified": "2026-11-01T00:00:00+00:00",
            }],
            "runs": [{"rid": "runs/mossy-match-1", "path": str(run), "status": "pass", "exports": 1,
                      "approved": 1 if s["review"] == "approve" else 0, "app": app}],
            "published": [], "configs": [],
            "campaigns": [{
                "ref": "p1:meta", "plan_id": "p1", "network": "meta", "name": "mm-test meta [cf:aaaa]",
                "mode": s["mode"], "status": s["status"], "daily": 20.0, "total_cap": 100.0, "currency": "USD",
                "start": "2026-11-16", "end_day": "2026-11-20", "store_url": STORE, "store_id": "demo.mossymatch",
                "created_at": "2026-11-15T10:00:00+00:00", "last_sync": {},
                "phrase": {"tr": "YAYINA AL 20.00 USD", "en": "ACTIVATE 20.00 USD"},
            }],
            "packs": [], "playables": [{"name": "mm-sample", "path": str(pl), "html": str(pl / "index.html"),
                                        "size": 80, "app": app, "builds": [], "checks": []}],
            "timeline": [{"at": "2026-10-20T08:00:00+00:00", "kind": "activated", "campaign": "p1:meta",
                          "campaign_name": "Meta_AEO_Tier1", "network": "meta", "plan": "p1", "mode": "mock",
                          "store_id": "demo.mossymatch", "detail": {"daily": 20.0}, "hash": "f" * 64, "seq": 1}],
            "audit": {"ok": True, "records": 1, "problem": "", "path": "audit.jsonl"},
        })  # fmt: skip
        return 0
    if args[0] == "make":
        prog = Path(arg(args, "--progress") or "")
        res = {
            "schema": "creative-forge/make/v1",
            "run_dir": str(run),
            "status": "pass",
            "exports": [{"file": "x"}],
        }
        prog.write_text(json.dumps({"state": "done", "done": 3, "total": 3, "message": "ready", "result": res}),
                        encoding="utf-8")  # fmt: skip
        out(res)
        return 0
    if args[0] == "review":
        s["review"] = "approve" if arg(args, "--approve") else "reject"
        save(s)
        out({"schema": "creative-forge/review/v1", "exports": []})
        return 0
    if args[0] == "publish":
        out({"schema": "creative-forge/publish/v1", "folder": arg(args, "--to"), "exports": 1})
        return 0
    if args[:2] == ["campaign", "init"]:
        out(
            {
                "schema": "creative-forge/campaign-init/v1",
                "config": str(ROOT / "campaigns" / "c.yaml"),
            }
        )
        return 0
    if args[:2] == ["campaign", "create"]:
        out({"schema": "creative-forge/campaign-create/v1", "ok": True, "active": False, "packs": ["/x/applovin"],
             "networks": [{"network": "meta", "created": 8, "stopped": None, "campaign": {"ref": "p1:meta"}}]})  # fmt: skip
        return 0
    if args[:2] == ["campaign", "activate"]:
        if s["mode"] == "live" and "--live" not in args:
            sys.stderr.write(
                "campaign: refused by a safety rule: meta is in live mode: real money. Pass --live\n"
            )
            return 2
        if arg(args, "--confirm") != "YAYINA AL 20.00 USD":
            sys.stderr.write("campaign: refused by a safety rule: confirmation does not match; "
                             "type exactly: YAYINA AL 20.00 USD\n")  # fmt: skip
            return 2
        s["status"] = "ACTIVE"
        save(s)
        out(
            {
                "schema": "creative-forge/campaign/v1",
                "campaign": {"ref": "p1:meta", "status": "ACTIVE"},
            }
        )
        return 0
    if args[:2] == ["campaign", "pause"]:
        s["status"] = "PAUSED"
        save(s)
        out(
            {
                "schema": "creative-forge/campaign/v1",
                "campaign": {"ref": "p1:meta", "status": "PAUSED"},
            }
        )
        return 0
    if args[:2] == ["playable", "pack"]:
        out({"schema": "creative-forge/playable-pack/v1", "network": arg(args, "--network"), "out": "x",
             "check": {"status": "pass"}})  # fmt: skip
        return 0
    if args[:2] == ["playable", "sample"]:
        out({"schema": "creative-forge/playable-sample/v1", "folder": "x", "html": "x"})
        return 0
    sys.stderr.write(f"fake forge: unknown command {args[:2]}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
