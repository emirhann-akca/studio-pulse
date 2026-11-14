"""Held-out evaluation of the analyst (brain/accuracy.py, eval/holdout.yaml, eval/dev.yaml).

The holdout file is frozen: its content hash is pinned here, the documented split rule is
recomputed, and no phrasing may also appear in the golden, dev or reachability sets. Ground
truth is SQL run on the demo warehouse by these tests, never a typed number.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from importlib import resources

import pytest

from studio_pulse.brain import accuracy, evaluate
from studio_pulse.brain.catalog import normalize
from studio_pulse.brain.executor import LockedSession
from studio_pulse.brain.qa import Answer

# sha256 of eval/holdout.yaml (LF line endings). Changing the holdout is a new holdout
# version: say so in docs/ANALYST.md and report numbers for both.
HOLDOUT_SHA256 = "aba0e801ce964c82399601ac5a37a6108afbcb3980fa51410b6267d9b125ecd0"
HOLDOUT_B_SHA256 = "a91f41b36fa3ecd4e0cfbaced89e161b2f44cfc3f81d73e9688ef9c2113a2d5a"
SALT = "studio-pulse-holdout-v1:"


def _text(name: str) -> str:
    raw = resources.files("studio_pulse.brain").joinpath("eval", name).read_text(encoding="utf-8")
    return raw.replace("\r\n", "\n")


def test_holdout_files_are_frozen():
    digest = hashlib.sha256(_text("holdout.yaml").encode("utf-8")).hexdigest()
    assert digest == HOLDOUT_SHA256, "holdout.yaml changed: it is frozen (docs/ANALYST.md)"
    digest = hashlib.sha256(_text("holdout_b.yaml").encode("utf-8")).hexdigest()
    assert digest == HOLDOUT_B_SHA256, "holdout_b.yaml changed: it is frozen (docs/ANALYST.md)"


def test_split_is_the_documented_rule():
    """Per kind, groups ordered by sha256(salt + group); the first ceil(60%) are held out.
    The audit's 16 probe questions (audit_*) were already seen and live in dev only."""
    hold = accuracy.load("holdout")["groups"]
    dev = [g for g in accuracy.load("dev")["groups"] if not g["group"].startswith("audit_")]
    assert len(hold) >= 80 and sum(len(g["q"]) for g in hold) >= 80
    for kind in ("value", "refuse", "why"):
        groups = [g["group"] for g in hold + dev if g["kind"] == kind]
        order = sorted(groups, key=lambda g: hashlib.sha256((SALT + g).encode()).hexdigest())
        k = math.ceil(0.6 * len(order))
        assert set(order[:k]) == {g["group"] for g in hold if g["kind"] == kind}, kind


def test_holdout_shares_no_question_with_the_other_sets():
    def phr(name: str) -> set[str]:
        if name == "golden":
            return {
                normalize(q)
                for i in evaluate.load_set("golden")["items"]
                for _, q in i["phrasings"]
            }
        if name == "reach":
            import yaml

            return {
                normalize(q) for _, q in yaml.safe_load(_text("reach.yaml"))["metrics"].values()
            }
        return {normalize(q) for g in accuracy.load(name)["groups"] for _, q in g["q"]}

    for name in ("holdout", "holdout_b"):
        hold = phr(name)
        for other in ("golden", "dev", "reach", "holdout_b" if name == "holdout" else "holdout"):
            assert not hold & phr(other), (name, other)
    assert not phr("dev") & phr("golden")
    groups = [g["group"] for n in accuracy.SETS for g in accuracy.load(n)["groups"]]
    assert len(groups) == len(set(groups))


def test_sets_are_bilingual_and_typed():
    for name in accuracy.SETS:
        its = accuracy.items(name)
        assert {i.lang for i in its} == {"en", "tr"}
        assert {i.kind for i in its} == {"value", "refuse", "why"}
        for i in its:
            assert (i.kind == "value") == bool(i.truth), i.group
            assert (i.kind == "refuse") == bool(i.absent), i.group
            assert (i.kind == "why") == bool(i.driver), i.group


@pytest.fixture(scope="module")
def wh(brain_demo, tmp_path_factory):
    return brain_demo.copy(tmp_path_factory.mktemp("accuracy"))


def test_ground_truth_is_computed_by_sql(wh):
    """Every truth query returns non-null values; every refusal item proves the data is absent."""
    with LockedSession.open(wh) as s:
        for name in accuracy.SETS:
            seen = set()
            for it in accuracy.items(name):
                if it.group in seen:
                    continue
                seen.add(it.group)
                t = accuracy.truth(s, it)
                if it.kind == "refuse":
                    assert t.absent_count == 0, it.group
                elif it.kind == "why":
                    assert t.driver, it.group
                else:
                    assert t.alternatives and all(t.alternatives), it.group
                    for cells in t.alternatives:
                        assert all(c is not None for c in cells), it.group


def _answer(text: str, lang: str = "en", route: str = "catalog", rows=None, cols=None) -> Answer:
    return Answer("q", lang, route, text=text, rows=rows or [], columns=cols or [])


def _item(kind: str = "value", **kw) -> accuracy.Item:
    base = dict(set="dev", group="g", kind=kind, lang="en", q="q")
    base.update(kw)
    return accuracy.Item(**base)


def test_grades():
    t = accuracy.Truth([[0.4131, 790]])
    it = _item(truth=("x",))
    assert accuracy.grade(_answer("CPI $0.413 (n = 790)."), it, t).grade == "right"
    assert accuracy.grade(_answer("CPI $0.413."), it, t).grade == "partial"
    assert accuracy.grade(_answer("CPI $0.588 (n = 1,217)."), it, t).grade == "wrong"
    assert accuracy.grade(_answer("No.", route="refused"), it, t).grade == "refused"
    tr = accuracy.Truth([[0.2635]])
    assert accuracy.grade(_answer("D1 %26,4 oldu.", "tr"), it, tr).grade == "right"
    assert accuracy.grade(_answer("D1 %26.4 oldu.", "tr"), it, tr).grade != "right"
    # a number only in a large table is not the answer
    rows = [[i, 0.1 * i] for i in range(10)] + [[99, 0.2635]]
    assert accuracy.grade(_answer("Many rows.", rows=rows), it, tr).grade == "partial"
    refuse = _item("refuse", absent="x")
    empty = accuracy.Truth([], absent_count=0)
    assert (
        accuracy.grade(_answer("No Meta spend.", route="refused"), refuse, empty).grade == "right"
    )
    assert accuracy.grade(_answer("CPI $0.413."), refuse, empty).grade == "wrong"
    why = _item("why", driver="x")
    drv = accuracy.Truth([], driver="1.8.0")
    assert accuracy.grade(_answer("Version 1.8.0 (low tier)."), why, drv).grade == "right"
    assert accuracy.grade(_answer("Version 1.8.01."), why, drv).grade == "partial"
    lvl = _item(truth=("x",), label_kind="level")
    lt = accuracy.Truth([["7"]])
    assert accuracy.grade(_answer("Most quit at level 7 (2,388)."), lvl, lt).grade == "right"
    lab = accuracy.Truth([["unity_ads"]])
    assert accuracy.grade(_answer("Cheapest: Unity Ads."), it, lab).grade == "right"
    amb = _item(truth=("x",), accept_refusal=True)
    assert accuracy.grade(_answer("Which one?", route="refused"), amb, t).grade == "right"


def test_wilson_intervals_in_the_report():
    rep = accuracy.Report("dev", "router")
    rep.rows = [
        accuracy.Graded(_item(), g, "catalog", "") for g in ["right"] * 8 + ["partial", "wrong"]
    ]
    s = rep.summary()
    assert s["right"]["n"] == 8 and s["wrong"]["n"] == 1
    lo, hi = s["right"]["ci95"]
    assert lo < 0.8 < hi and 0.45 < lo < 0.5  # Wilson 95% for 8/10: 0.490-0.943
    assert "CONFIDENTLY WRONG" in rep.table()


def test_router_is_never_confidently_wrong_on_the_dev_set(wh, no_network):
    rep = accuracy.run(wh, "dev")
    c = rep.counts()
    assert c["wrong"] == 0, [(g.item.q, g.detail) for g in rep.rows if g.grade == "wrong"]
    assert c["right"] >= 85


@pytest.mark.parametrize("name, min_right", [("holdout", 109), ("holdout_b", 41)])
def test_router_on_the_holdouts(wh, no_network, name, min_right):
    """A regression guard, not a target: the floors are the 14 Nov 2026 results after the
    confidently wrong answers of the first run were inspected (docs/ANALYST.md has the
    clean numbers too). Confidently wrong must stay at zero."""
    rep = accuracy.run(wh, name)
    c = rep.counts()
    assert c["wrong"] == 0, [(g.item.q, g.detail) for g in rep.rows if g.grade == "wrong"]
    assert c["right"] >= min_right


def test_pulse_ask_eval_command(brain_demo, capsys):
    from studio_pulse import cli

    rc = cli.main(
        [
            "ask",
            "--eval",
            "--set",
            "dev",
            "--no-llm",
            "--json",
            "--config",
            str(brain_demo.config_path),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    rep = out["reports"][0]
    assert rep["set"] == "dev" and rep["mode"].startswith("router")
    assert rep["questions"] == len(out["questions"]) and rep["wrong"]["n"] == 0


def test_as_of_comes_from_the_set():
    assert accuracy.as_of("holdout") == dt.date(2026, 11, 1) == accuracy.as_of("dev")
