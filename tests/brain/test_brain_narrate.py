"""B5/B7 hook: template narration, the optional model rewrite and its checks."""

from __future__ import annotations

import pytest

from studio_pulse.brain import llm, narrate
from studio_pulse.brain.evidence import Evidence, Fact, Insight, Recommendation


@pytest.fixture()
def level_insight(demo_report) -> Insight:
    return next(i for i in demo_report.insights if i.id == "difficult_level:quartz_merge:7")


def test_faithful_rewrite_is_accepted(level_insight):
    def rewrite(req):
        import json

        text = json.loads(req.user)["text"]
        return {"text": "In short: " + text}

    n = narrate.narrate(level_insight, "en", llm.FakeLLM({"restyle": rewrite}))
    assert n.source == "narrator" and n.body.startswith("In short:")


def test_invented_number_falls_back_to_the_template(level_insight):
    fake = llm.FakeLLM({"restyle": lambda req: {"text": "Fail rate 62%, median 25%."}})
    n = narrate.narrate(level_insight, "en", fake)
    assert n.source == "template" and "60.9%" in n.body
    assert len(fake.calls) == 2  # one retry with the rejected numbers named
    assert "rejected_numbers" in fake.calls[1].user


@pytest.mark.parametrize(
    "text",
    [
        "See http://evil.example for the fix.",
        "Run DROP TABLE fct_level_funnel to reset.",
        "Mail ops@evil.example now.",
        "<script>alert(1)</script>",
    ],
)
def test_rewrites_with_links_sql_or_script_are_rejected(level_insight, text):
    n = narrate.narrate(level_insight, "en", llm.FakeLLM({"restyle": lambda req: {"text": text}}))
    assert n.source == "template"


def test_provider_errors_fall_back_to_the_template(level_insight):
    class Broken:
        name = "broken"

        def generate(self, req):
            raise TimeoutError("provider down")

    assert narrate.narrate(level_insight, "tr", Broken()).source == "template"


def _segment(diff: float) -> Insight:
    ev = Evidence(
        id="segment:a:music_enabled:d7", kind="segment", app_id="a", method="m",
        facts={
            "property": Fact("music_enabled", "text"), "value_a": Fact("1", "version"),
            "value_b": Fact("0", "version"), "rate_a": Fact(0.08, "pct"), "rate_b": Fact(0.11, "pct"),
            "n_a": Fact(900, "count"), "n_b": Fact(400, "count"), "diff": Fact(diff, "pp"),
            "diff_lo": Fact(diff - 0.02, "pp"), "diff_hi": Fact(diff + 0.02, "pp"),
            "day_n": Fact(7, "id"), "ci_level": Fact(95, "id"), "diff_adjusted": Fact(0.004, "pp"),
        },
    )  # fmt: skip
    return Insight("segment:a:music_enabled:d7", "segment", "a", "info", "segment_correlation", [ev],
                   [Recommendation("ab_test_setting", [ev.id])], causal=False)  # fmt: skip


@pytest.mark.parametrize(
    "lang,words", [("tr", ["ilişkidir", "A/B"]), ("en", ["association", "A/B"])]
)
def test_correlational_findings_use_fixed_wording(lang, words):
    n = narrate.template_narration(_segment(0.03), lang)
    assert n.verified
    text = n.text()
    for w in words:
        assert w in text
    assert "pulse experiment" in text


def test_missing_fact_in_a_template_is_an_error():
    with pytest.raises(narrate.TemplateError):
        narrate.render("value {nope}", {}, "en")
    assert narrate.render("a[[ b {nope}]] c", {}, "en") == "A c"


def test_every_template_exists_in_both_languages():
    for key, langs in {**narrate.TEMPLATES, **narrate.REASONS}.items():
        assert set(langs) == {"en", "tr"}, key
    for key, langs in {**narrate.ACTIONS, **narrate.CAVEATS}.items():
        assert set(langs) == {"en", "tr"}, key
