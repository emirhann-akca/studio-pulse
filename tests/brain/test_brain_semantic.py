"""The semantic layer (brain/semantic.py): generated from docs/METRICS.md and the catalog,
grains checked on the demo warehouse, every catalog metric reachable by a question."""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from studio_pulse.brain import catalog, semantic
from studio_pulse.brain.executor import ALLOWED_TABLES, LockedSession
from studio_pulse.brain.qa import Router

from .conftest import AS_OF

ROOT = Path(__file__).resolve().parents[2]


def test_metric_docs_are_generated_from_metrics_md():
    text = (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
    want = semantic.render_docs(text)
    have = (
        resources.files("studio_pulse.brain")
        .joinpath(semantic.DOCS_FILE)
        .read_text(encoding="utf-8")
        .replace("\r\n", "\n")
    )
    assert have == want, "run: python -m studio_pulse.brain.semantic --write"
    docs = semantic.docs_from_markdown(text)
    assert docs["fct_app_daily.dau"]["formula"] == "distinct active users on d"
    assert "fct_ua_cohort_roas.roas_d30" in docs  # roas_d0/d7/d30 expands


def test_every_metrics_md_metric_is_in_the_catalog_and_the_layer():
    cat = catalog.load()
    covered = {f"{m.model}.{m.value}" for m in cat.metrics.values()}
    for m in cat.metrics.values():
        for expr in (m.numerator or "", m.denominator or ""):
            covered |= {f"{m.model}.{c}" for c in re.findall(r"[a-z_][a-z0-9_]*", expr)}
    missing = sorted(set(semantic.metric_docs()) - covered - set(cat.not_metrics))
    assert not missing, missing
    for m in cat.metrics.values():
        assert m.model in semantic.MODELS, m.id


@pytest.fixture(scope="module")
def wh(brain_demo, tmp_path_factory):
    return brain_demo.copy(tmp_path_factory.mktemp("semantic"))


def test_declared_grains_are_unique_on_the_demo(wh):
    with LockedSession.open(wh) as s:
        tables = {
            r[0]
            for r in s.con.execute("SELECT table_name FROM information_schema.tables").fetchall()
        }
        for model, spec in semantic.MODELS.items():
            if model not in tables:
                continue
            keys = ", ".join(spec.grain)
            dup = s.con.execute(
                f"SELECT COUNT(*) FROM (SELECT {keys} FROM {model} GROUP BY ALL HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            assert dup == 0, (model, spec.grain)
            if spec.time:
                cols = {r[0] for r in s.con.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [model]
                ).fetchall()}  # fmt: skip
                assert spec.time in cols, model


def test_the_models_context_is_a_semantic_layer(wh):
    with LockedSession.open(wh) as s:
        r = Router(s, as_of=AS_OF)
        ctx = r.semantic_context()
    assert ctx["today"] == "2026-11-01" and ctx["newest_data_day"] == "2026-10-31"
    assert set(ctx["tables"]) <= set(ALLOWED_TABLES)
    cpi = ctx["metrics"]["ua_cpi_usd"]
    assert "SUM(spend_usd)" in cpi and "stg_ua_spend adds country, platform" in cpi
    assert ctx["tables"]["stg_ua_spend"]["values"]["network"] == [
        "applovin", "google_ads", "meta", "tiktok", "unity_ads"
    ]  # fmt: skip
    assert "28" in ctx["tables"]["fct_perf_by_tier"]["window"]
    assert any("never drop a filter" in rule for rule in ctx["rules"])
    assert len(json.dumps(ctx)) < 60_000  # about 15k tokens


def test_every_catalog_metric_is_reachable_by_a_question(wh, no_network):
    """reach.yaml has a question per catalog metric; asked without a model, each is answered
    from that metric's table (or refused as "no data" when the demo has none for it)."""
    reach = yaml.safe_load(
        resources.files("studio_pulse.brain")
        .joinpath("eval", "reach.yaml")
        .read_text(encoding="utf-8")
    )["metrics"]
    cat = catalog.load()
    assert set(reach) == set(cat.metrics)
    bad = []
    with LockedSession.open(wh) as s:
        r = Router(s, as_of=AS_OF)
        for mid, (lang, q) in reach.items():
            m = cat.get(mid)
            a = r.ask(q, lang)
            if a.route == "refused":
                has = s.con.execute(
                    f"SELECT COUNT({m.value}) FROM {m.model} WHERE {m.where()}"
                ).fetchone()[0]
                if a.refusal == "no_data" and not has:
                    continue
                bad.append((mid, q, a.refusal))
                continue
            reads = (a.matched or "").split(":")[0] == mid or (
                m.model in a.sql and re.search(r"\b" + re.escape(m.value.split()[0]), a.sql)
            )
            if not (reads and a.verified):
                bad.append((mid, q, a.matched))
    assert not bad, bad
