"""Held-out accuracy of the analyst: answers graded against SQL ground truth.

The golden set (eval/golden.yaml) checks the router on the questions it was
built for. This module measures it on questions it was *not* tuned on:
`eval/holdout.yaml` (set A, frozen before the router was changed; see
docs/ANALYST.md for the split), `eval/holdout_b.yaml` (set B, written after the
changes and frozen before it was ever run on them) and `eval/dev.yaml` (the
part the router may be improved against). Each group of
phrasings carries its ground truth as SQL over the demo warehouse, run here,
never typed by hand:

    kind: value    truth: one or more alternative SELECTs (a question such as
                   "last week" has more than one fair reading); every cell of
                   the first row(s) is a required value (numbers) or label
                   (strings, dates)
    kind: refuse   absent: a SELECT that must return 0 - the data the question
                   asks about does not exist, so the right answer is a refusal
    kind: why      driver: a SELECT returning the segment that moved the
                   metric (e.g. the release); the answer must name it

Grades, per question:
    right     every required value / label of some alternative appears in the
              answer (text, or a result table of at most 5 rows); for refuse
              items a refusal; for why items the driver is named
    partial   some but not all of them; a number only in a large table; a
              why answer without the driver
    refused   the analyst declined (counts as right when `accept_refusal`)
    wrong     an answer that states numbers and none of them is the truth, or
              numbers where the data does not exist: CONFIDENTLY WRONG
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from . import stats, verify
from .catalog import normalize
from .executor import LockedSession
from .qa import Answer, Router

GRADES = ("right", "partial", "refused", "wrong")
SETS = ("dev", "holdout", "holdout_b")
SMALL_TABLE = 5


@dataclass(frozen=True)
class Item:
    set: str
    group: str
    kind: str  # value | refuse | why
    lang: str
    q: str
    truth: tuple[str, ...] = ()
    absent: str | None = None
    driver: str | None = None
    accept_refusal: bool = False
    label_kind: str | None = None  # "level": bare numbers count as labels near a level word


@cache
def load(name: str) -> dict[str, Any]:
    text = (
        resources.files("studio_pulse.brain")
        .joinpath("eval", f"{name}.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def items(name: str) -> list[Item]:
    out = []
    for g in load(name)["groups"]:
        for lang, q in g["q"]:
            out.append(
                Item(
                    set=name,
                    group=g["group"],
                    kind=g["kind"],
                    lang=lang,
                    q=q,
                    truth=tuple(g.get("truth") or ()),
                    absent=g.get("absent"),
                    driver=g.get("driver"),
                    accept_refusal=bool(g.get("accept_refusal")),
                    label_kind=g.get("label_kind"),
                )
            )
    return out


def as_of(name: str) -> dt.date:
    v = load(name)["as_of"]
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


@dataclass
class Truth:
    alternatives: list[list[Any]]  # each: the required cells
    absent_count: int | None = None
    driver: str | None = None


def truth(session: LockedSession, item: Item) -> Truth:
    if item.kind == "refuse":
        assert item.absent
        n = session.run_trusted(item.absent).rows[0][0]
        return Truth([], absent_count=int(n or 0))
    if item.kind == "why":
        assert item.driver
        rows = session.run_trusted(item.driver).rows
        return Truth([], driver=str(rows[0][0]) if rows and rows[0][0] is not None else None)
    alts = []
    for sql in item.truth:
        res = session.run_trusted(sql)
        cells = [c for r in res.rows for c in r]
        alts.append(cells)
    return Truth(alts)


@dataclass
class Graded:
    item: Item
    grade: str
    route: str
    text: str
    detail: str = ""
    refusal: str | None = None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _num_in_text(v: float, toks: list[verify.Token]) -> bool:
    a = abs(float(v))
    for t in toks:
        if t.value != t.value or t.points or t.bound:
            continue
        tol = 0.5 * 10 ** (-t.decimals) + 1e-9
        if t.percent:
            if abs(a * 100 - t.value) <= tol:
                return True
            continue
        if abs(a - t.value) <= tol:
            return True
    return False


def _num_in_rows(v: float, rows: list[list[Any]]) -> bool:
    a = float(v)
    for r in rows:
        for c in r:
            if _is_number(c) and abs(float(c) - a) <= 1e-6 * max(1.0, abs(a)):
                return True
    return False


_CONNECTORS = frozenset({"and", "ve", "of", "the", "or", "veya"})
_LEVEL_WORD = re.compile(r"\b(level|levels|seviye\w*|bolum\w*)\b")


def _label_in_text(label: str, text: str, raw: str, level: bool) -> bool:
    if isinstance(label, dt.date):
        return label.isoformat() in raw
    s = str(label)
    if re.fullmatch(r"\d{4}-\d\d-\d\d", s):
        return s in raw
    norm = f" {normalize(text)} "
    lab = normalize(s.replace("_", " "))
    if level:
        return (
            bool(_LEVEL_WORD.search(norm))
            and re.search(rf"(?<![\d.]){re.escape(lab)}(?![\d.])", norm) is not None
        )
    if re.fullmatch(r"[\d.]+", lab):  # a version string: exact digits, not a part of another
        return re.search(rf"(?<![\d.]){re.escape(lab)}(?![\d.])", raw) is not None
    if f" {lab} " in norm:
        return True
    # "crashes_bugs" is shown as "Crashes and bugs": compare without connector words
    drop = lambda x: " ".join(w for w in x.split() if w not in _CONNECTORS)  # noqa: E731
    return f" {drop(lab)} " in f" {drop(norm)} "


def _matches(cells: list[Any], a: Answer, item: Item) -> tuple[int, int, bool]:
    """(found, required, found_only_in_a_big_table)."""
    toks = verify.tokens(a.text, a.lang)
    small = len(a.rows) <= SMALL_TABLE
    found = 0
    big_only = False
    for c in cells:
        if _is_number(c):
            if _num_in_text(c, toks) or (small and _num_in_rows(c, a.rows)):
                found += 1
            elif _num_in_rows(c, a.rows):
                big_only = True
        elif c is not None:
            if _label_in_text(c, a.text, a.text, item.label_kind == "level"):
                found += 1
    return found, len(cells), big_only


def _states_numbers(a: Answer) -> bool:
    return bool(a.rows) or bool(verify.tokens(a.text, a.lang))


def grade(a: Answer, item: Item, t: Truth) -> Graded:
    def g(grade: str, detail: str = "") -> Graded:
        return Graded(item, grade, a.route, a.text, detail, a.refusal)

    refused = a.route == "refused"
    if item.kind == "refuse":
        if refused:
            return g("right", f"refused ({a.refusal})")
        if not _states_numbers(a):
            return g("right", "answered without numbers")
        return g("wrong", "stated numbers for data that does not exist")
    if refused:
        if item.accept_refusal:
            return g("right", f"refused ({a.refusal}), accepted for this question")
        return g("refused", f"refused ({a.refusal})")
    if item.kind == "why":
        if t.driver and _label_in_text(t.driver, a.text, a.text, False):
            return g("right", f"names {t.driver}")
        return g("partial", f"does not name the driver {t.driver}")
    best = (0, 1, False)
    for cells in t.alternatives:
        f, n, big = _matches(cells, a, item)
        if n and (f / n > best[0] / best[1] or (f == best[0] and big)):
            best = (f, n, big)
        if f == n and n:
            return g("right", f"{f}/{n} values")
    f, n, big = best
    if f > 0 or big:
        return g("partial", f"{f}/{n} values" + (" (only in a large table)" if big else ""))
    if not _states_numbers(a):
        return g("partial", "answered without the value")
    expected = t.alternatives[0] if t.alternatives else []
    return g("wrong", f"none of {expected[:4]} in the answer")


@dataclass
class Report:
    set: str
    mode: str
    rows: list[Graded] = field(default_factory=list)
    cost_usd: float | None = None

    def counts(self) -> Counter[str]:
        return Counter(r.grade for r in self.rows)

    def summary(self) -> dict[str, Any]:
        n = len(self.rows)
        c = self.counts()
        out: dict[str, Any] = {"set": self.set, "mode": self.mode, "questions": n}
        for k in GRADES:
            lo, hi = stats.wilson(c[k], n) if n else (math.nan, math.nan)
            out[k] = {"n": c[k], "rate": c[k] / n if n else math.nan, "ci95": [lo, hi]}
        out["groups"] = len({r.item.group for r in self.rows})
        if self.cost_usd is not None:
            out["cost_usd"] = self.cost_usd
        return out

    def table(self) -> str:
        s = self.summary()
        lines = [f"{self.set} set, {s['questions']} questions ({s['groups']} groups), {self.mode}"]
        for k in GRADES:
            x = s[k]
            lo, hi = x["ci95"]
            name = "CONFIDENTLY WRONG" if k == "wrong" else k
            lines.append(
                f"  {name:<18} {x['n']:>3}  {x['rate'] * 100:5.1f}%  (95% Wilson {lo * 100:.1f}-{hi * 100:.1f}%)"
            )
        return "\n".join(lines)


def run(
    path: str | Path,
    name: str,
    *,
    client: Any = None,
    mode: str = "router",
    on_item: Any = None,
) -> Report:
    rep = Report(name, mode)
    with LockedSession.open(path) as s:
        router = Router(s, client=client, as_of=as_of(name))
        for it in items(name):
            t = truth(s, it)
            a = router.ask(it.q, it.lang)
            gr = grade(a, it, t)
            rep.rows.append(gr)
            if on_item is not None:
                on_item(gr)
    return rep
