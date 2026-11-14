"""The brain on the shared LLM layer: config, request mapping, budget, ledger, fallbacks."""

from __future__ import annotations

import json

import pytest

from studio_pulse.brain import llm, narrate
from studio_pulse.llm import FakeProvider, LLMConfigError

NO_ENV: dict[str, str] = {}


def test_off_and_fake_need_no_shared_layer(tmp_path):
    assert llm.client_from_config(None, env=NO_ENV) is None
    assert llm.client_from_config({"provider": "none"}, env=NO_ENV) is None
    assert isinstance(llm.client_from_config({"provider": "fake"}, env=NO_ENV), llm.FakeLLM)
    # `shared` with a fake llm: block is the brain's own deterministic fake
    c = llm.client_from_config(
        {"provider": "shared"}, shared_block={"provider": "fake"}, state_dir=tmp_path, env=NO_ENV
    )
    assert isinstance(c, llm.FakeLLM)


def test_shared_without_an_llm_block_stays_off(tmp_path):
    assert llm.client_from_config({"provider": "shared"}, state_dir=tmp_path, env=NO_ENV) is None


def test_bad_settings_raise():
    with pytest.raises(ValueError):
        llm.client_from_config({"provider": "gpt"}, env=NO_ENV)
    with pytest.raises(LLMConfigError):
        llm.client_from_config({"provider": "shared"}, shared_block={"provider": "x"}, env=NO_ENV)


def test_environment_wins_once_the_brain_asks_for_a_model(tmp_path):
    c = llm.client_from_config(
        {"provider": "gemini"}, state_dir=tmp_path, env={"PULSE_LLM_PROVIDER": "fake"}
    )
    assert isinstance(c, llm.FakeLLM)


def _shared(tmp_path, answer, **block):
    prov = FakeProvider(answer, price=(1.0, 1.0))
    c = llm.client_from_config(
        {"provider": "shared", **block},
        shared_block={"provider": "fake", "model": "fake-1"},
        state_dir=tmp_path,
        env=NO_ENV,
        provider_obj=prov,
    )
    assert isinstance(c, llm.SharedLLM)
    return c, prov


def test_requests_carry_the_purpose_schema_and_budget(tmp_path):
    c, prov = _shared(tmp_path, lambda req: {"metric": "dau", "app": None, "by": None, "days": 7})
    reply = c.generate(llm.request("route", llm.SYSTEM_ROUTE, {"question": "DAU?"}))
    assert reply.data == {"metric": "dau", "app": None, "by": None, "days": 7}
    sent = prov.calls[0]
    assert sent.purpose == "brain.route" and sent.schema == llm.SCHEMAS["route"]
    assert json.loads(sent.prompt) == {"question": "DAU?"}
    assert c.client.budget_usd == llm.DEFAULT_BUDGET_USD
    ledger = tmp_path / "brain" / "llm-ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[-1]["purpose"] == "brain.route"
    # the same request again comes from the cache: no second provider call
    c.generate(llm.request("route", llm.SYSTEM_ROUTE, {"question": "DAU?"}))
    assert len(prov.calls) == 1


def test_brain_budget_overrides_the_shared_block(tmp_path):
    c, _ = _shared(tmp_path, lambda req: {"sql": None}, budget_usd=0.05)
    assert c.client.budget_usd == pytest.approx(0.05)


def test_restyle_through_the_shared_layer_keeps_numbers_or_falls_back(tmp_path, demo_report):
    ins = next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")
    base = narrate.template_narration(ins, "en")

    good, _ = _shared(tmp_path / "a", lambda req: {"text": json.loads(req.prompt)["text"]})
    assert narrate.narrate(ins, "en", good).body == base.body

    # a model that changes a number is rejected twice, then the template is used
    bad, prov = _shared(tmp_path / "b", lambda req: {"text": "Level 7 fails 99.9% of the time."})
    out = narrate.narrate(ins, "en", bad)
    assert out.body == base.body and len(prov.calls) == 2


def test_a_provider_failure_falls_back_to_the_template(tmp_path, demo_report):
    from studio_pulse.llm import TransientLLMError

    ins = next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")
    prov = FakeProvider(errors=[TransientLLMError("503")] * 10, price=(1.0, 1.0))
    c = llm.client_from_config(
        {"provider": "shared", "max_retries": 0},
        shared_block={"provider": "fake"},
        state_dir=tmp_path,
        env=NO_ENV,
        provider_obj=prov,
    )
    assert narrate.narrate(ins, "en", c).body == narrate.template_narration(ins, "en").body


def test_a_turkish_rewrite_without_turkish_letters_is_rejected(tmp_path, demo_report):
    """14 Nov, real model: "Zevo D1 elde tutma orani ... gerceklesmistir" (ASCII Turkish) passed
    the number check. A Turkish rewrite that drops the Turkish letters keeps the template."""
    ins = next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")
    base = narrate.template_narration(ins, "tr")
    ascii_tr = base.body.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))
    assert ascii_tr != base.body
    bad, prov = _shared(tmp_path / "c", lambda req: {"text": ascii_tr})
    assert narrate.narrate(ins, "tr", bad).body == base.body and len(prov.calls) == 2
    good, _ = _shared(tmp_path / "d", lambda req: {"text": json.loads(req.prompt)["text"]})
    assert narrate.narrate(ins, "tr", good).body == base.body
    assert not narrate._lost_letters("ascii only text", "ascii only text", "tr")
