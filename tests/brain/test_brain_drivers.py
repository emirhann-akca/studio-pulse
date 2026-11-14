"""Why questions (brain/drivers.py): the segment that moved the metric, with its share."""

from __future__ import annotations

import pytest

from studio_pulse.brain import catalog, drivers
from studio_pulse.brain.executor import LockedSession
from studio_pulse.brain.qa import Router

from .conftest import AS_OF


@pytest.fixture(scope="module")
def wh(brain_demo, tmp_path_factory):
    return brain_demo.copy(tmp_path_factory.mktemp("drivers"))


def test_the_planted_release_is_named(wh, no_network):
    """Demo: version 1.8.0 of quartz_merge lowers D1, raises crashes and low-tier frame time."""
    with LockedSession.open(wh) as s:
        r = Router(s, as_of=AS_OF)
        d1 = r.ask("Why did quartz_merge's D1 retention drop?", "en")
        d1_tr = r.ask("quartz_merge'de D1 neden düştü?", "tr")
        crash = r.ask("Why did crash-free users of quartz_merge fall?", "en")
        frame = r.ask("Why is quartz_merge's median frame time higher?", "en")
    for a in (d1, d1_tr, crash, frame):
        assert a.route == "catalog" and "drivers" in a.notes and a.verified, a.text
        assert "1.8.0" in a.text, a.text
    assert "app version = 1.8.0" in d1.text and "Association in time" in d1.text
    assert "uygulama sürümü = 1.8.0" in d1_tr.text and "Zamansal ilişki" in d1_tr.text
    assert "first appeared on 2026-10-28" in crash.text  # crash tables carry no version
    assert "low tier" in frame.text and "1.7.2" in frame.text


def test_additive_splits_add_up(wh):
    with LockedSession.open(wh) as s:
        cat = catalog.load()
        res = drivers.explain(s, cat, cat.get("revenue_usd"), "pacer_pal", as_of=AS_OF)
        dau = drivers.explain(s, cat, cat.get("dau"), "mossy_match", as_of=AS_OF)
    assert res is not None and res.dimension == "component"
    rows = res.evidence[0].rows
    assert sum(r[3] for r in rows) == pytest.approx(res.value1 - res.value0)
    assert dau is not None and dau.dimension == "platform"
    assert sum(r[3] for r in dau.evidence[0].rows) == pytest.approx(dau.value1 - dau.value0)


def test_the_question_direction_is_checked():
    assert drivers.asked_direction("why did d1 drop", "up") == "down"
    assert drivers.asked_direction("why is frame time worse", "down") == "up"
    assert drivers.asked_direction("neden kotulesti", "up") == "down"
    assert drivers.asked_direction("why did it change", "up") is None
