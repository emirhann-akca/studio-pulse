"""Shared LLM provider layer: cache, budget, ledger, repair, retries, providers, secrets."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
import requests

from studio_pulse import llm
from studio_pulse.connectors.replay import ReplaySession
from studio_pulse.llm import (
    AnthropicProvider,
    BudgetExceeded,
    FakeProvider,
    GeminiVertexProvider,
    LLMClient,
    LLMError,
    LLMOutputError,
    LLMRequest,
    OpenAICompatibleProvider,
    TransientLLMError,
    redact,
)
from studio_pulse.llm.jsonschema import minimal_instance, parse_json_text, validate
from studio_pulse.llm.providers import to_anthropic_schema, to_vertex_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["a", "b"]},
        "notes": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 2},
    },
    "required": ["label", "notes"],
    "additionalProperties": False,
}


def req(prompt: str = "classify this", **kw) -> LLMRequest:
    return LLMRequest(prompt=prompt, system="be brief", schema=SCHEMA, **kw)


def test_validator_and_json_reader():
    assert validate({"label": "a", "notes": []}, SCHEMA) == []
    errs = validate({"label": "c", "notes": ["x" * 30], "extra": 1}, SCHEMA)
    assert any("not one of" in e for e in errs)
    assert any("longer than" in e for e in errs)
    assert any("unexpected key" in e for e in errs)
    assert validate({"notes": []}, SCHEMA) == ["$: missing 'label'"]
    assert validate(True, {"type": "integer"})  # bool is not an integer
    assert parse_json_text('```json\n{"label": "a"}\n```') == {"label": "a"}
    assert parse_json_text('Sure! {"label": "b"} done') == {"label": "b"}
    assert validate(minimal_instance(SCHEMA), SCHEMA) == []


def test_cache_means_the_same_input_is_never_billed_twice(tmp_path):
    prov = FakeProvider(lambda r: {"label": "a", "notes": []}, price=(1.0, 2.0))
    c = LLMClient(prov, cache_dir=tmp_path / "c", ledger_path=tmp_path / "l.jsonl")
    r1 = c.generate(req())
    r2 = c.generate(req())
    assert r1.data == r2.data == {"label": "a", "notes": []}
    assert len(prov.calls) == 1 and r2.cached and not r1.cached
    assert r1.cost_usd > 0 and r2.cost_usd == 0
    rows = [json.loads(x) for x in (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["cached"] for r in rows] == [False, True]
    # a different prompt is a different key
    c.generate(req("another game"))
    assert len(prov.calls) == 2
    # a new client over the same cache dir still does not call the model
    prov2 = FakeProvider(lambda r: {"label": "b", "notes": []})
    c2 = LLMClient(prov2, cache_dir=tmp_path / "c")
    assert c2.generate(req()).data["label"] == "a" and not prov2.calls


def test_budget_cap_refuses_before_sending(tmp_path):
    prov = FakeProvider(lambda r: {"label": "a", "notes": []}, price=(1000.0, 1000.0))
    c = LLMClient(prov, budget_usd=0.01)
    with pytest.raises(BudgetExceeded):
        c.generate(req(max_output_tokens=2048))
    assert prov.calls == [] and c.spent_usd == 0
    # a cheap model fits, and spending accumulates up to the cap
    cheap = FakeProvider(lambda r: {"label": "a", "notes": []}, price=(10.0, 10.0))
    c2 = LLMClient(cheap, budget_usd=0.003)
    for i in range(40):
        try:
            c2.generate(req(f"game {i}", max_output_tokens=50))
        except BudgetExceeded:
            break
    assert c2.spent_usd <= 0.003 and 1 <= len(cheap.calls) < 40


def test_unknown_price_is_refused_while_a_budget_is_set():
    prov = FakeProvider(model="mystery-model", price=None)
    with pytest.raises(BudgetExceeded, match="no price"):
        LLMClient(prov, budget_usd=1.0).generate(req())
    # without a budget, or with a configured price, it runs
    assert LLMClient(prov).generate(req()).data is not None
    c = LLMClient(prov, budget_usd=1.0, prices={"mystery-model": [0.1, 0.2]})
    assert c.generate(req()).cost_usd >= 0


def test_repair_once_then_give_up():
    answers = iter(["not json at all", json.dumps({"label": "b", "notes": []})])
    prov = FakeProvider(lambda r: next(answers))
    res = LLMClient(prov).generate(req())
    assert res.repaired and res.data["label"] == "b" and len(prov.calls) == 2
    assert "Your previous answer could not be used" in prov.calls[1].prompt
    bad = FakeProvider(lambda r: {"label": "zzz", "notes": []})
    with pytest.raises(LLMOutputError):
        LLMClient(bad).generate(req())
    assert len(bad.calls) == 2  # one repair, no more


def test_transient_errors_are_retried_with_backoff(tmp_path):
    waits = []
    prov = FakeProvider(
        lambda r: {"label": "a", "notes": []},
        errors=[TransientLLMError("HTTP 429"), TransientLLMError("HTTP 503")],
    )
    c = LLMClient(prov, sleep=waits.append, backoff_s=1.0, ledger_path=tmp_path / "l.jsonl")
    assert c.generate(req()).data["label"] == "a"
    assert waits == [1.0, 2.0]
    oks = [
        json.loads(x)["ok"] for x in (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert oks == [False, False, True]
    always = FakeProvider(errors=[TransientLLMError("HTTP 503")] * 5)
    with pytest.raises(TransientLLMError):
        LLMClient(always, sleep=lambda s: None, max_retries=2).generate(req())
    assert len(always.calls) == 3
    fatal = FakeProvider(errors=[LLMError("HTTP 400: bad request")])
    with pytest.raises(LLMError):
        LLMClient(fatal, sleep=lambda s: None).generate(req())
    assert len(fatal.calls) == 1


SECRET = "sk-test-SECRETSECRET1234567890"


def test_secrets_never_reach_errors_logs_ledgers_or_caches(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    body = {"error": {"message": f"Incorrect API key provided: {SECRET}"}}
    sess = ReplaySession(
        tmp_path,
        entries=[{"method": "POST", "path": "/v1/chat/completions", "status": 401, "body": body}],
    )
    prov = OpenAICompatibleProvider(
        "qwen-lora", base_url="http://gpu.local:8081/v1", api_key=SECRET, session=sess
    )
    c = LLMClient(prov, cache_dir=tmp_path / "cache", ledger_path=tmp_path / "ledger.jsonl")
    with pytest.raises(LLMError) as exc:
        c.generate(req())
    assert SECRET not in str(exc.value) and "[REDACTED]" in str(exc.value)
    # the key was sent where it belongs (the header), never in the URL or body
    sent = sess.recorded[0]
    assert sent.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in sent.url and SECRET not in json.dumps(sent.body)
    assert SECRET not in repr(prov) and SECRET not in repr(c.provider)
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            assert SECRET not in p.read_text(encoding="utf-8"), p
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(c.entries)
    # the project id of a Vertex call is redacted from its errors too
    gsess = ReplaySession(
        tmp_path,
        entries=[
            {
                "method": "POST",
                "path": "/v1/projects/my-secret-proj/"
                "locations/global/publishers/google/models/gemini-3.5-flash-lite:generateContent",
                "status": 403,
                "body": {"error": {"message": "Permission denied on project my-secret-proj"}},
            }
        ],
    )
    g = GeminiVertexProvider("gemini-3.5-flash-lite", project="my-secret-proj", session=gsess)
    with pytest.raises(LLMError) as gexc:
        LLMClient(g).generate(req())
    assert "my-secret-proj" not in str(gexc.value)


def test_redact_patterns():
    google_key = "AI" + "zaSyA1234567890abcdefghijklmnop"  # built so secret scanners skip the file
    text = (
        "Authorization: Bearer ya29.a0AfH6SMBx-abc.def key=" + google_key + " "
        "x-api-key: sk-ant-api03-abcdefghijklmnop token=abcdef123456"
    )
    out = redact(text)
    for leaked in ("ya29.a0AfH6SMBx", google_key[:10], "sk-ant-api03", "abcdef123456"):
        assert leaked not in out
    assert redact("my value hunter2xyz", ["hunter2xyz"]) == "my value [REDACTED]"


def test_gemini_request_shape_and_usage(tmp_path):
    url = (
        "/v1/projects/p1/locations/global/publishers/google/models/"
        "gemini-3.5-flash-lite:generateContent"
    )
    reply = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "thinking...", "thought": True},
                        {"text": '{"label": "a", "notes": []}'},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 10,
        },
        "modelVersion": "gemini-3.5-flash-lite",
    }
    sess = ReplaySession(tmp_path, entries=[{"method": "POST", "path": url, "body": reply}])
    g = GeminiVertexProvider("gemini-3.5-flash-lite", project="p1", session=sess)
    c = LLMClient(g, budget_usd=0.30)
    res = c.generate(req(images=[llm.ImagePart(b"\x89PNG", "image/png")]))
    assert res.data == {"label": "a", "notes": []}
    assert (res.input_tokens, res.output_tokens) == (120, 40)  # thinking tokens bill as output
    assert res.cost_usd == pytest.approx((120 * 0.30 + 40 * 2.50) / 1e6)
    body = sess.recorded[0].body
    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseSchema"]["type"] == "OBJECT"
    assert gen["responseSchema"]["properties"]["label"]["enum"] == ["a", "b"]
    assert "additionalProperties" not in json.dumps(gen["responseSchema"])
    assert gen["thinkingConfig"] == {"thinkingLevel": "low"}
    assert body["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/png"
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert sess.recorded[0].headers["x-goog-user-project"] == "p1"


def test_gemini_drops_thinking_config_when_the_model_rejects_it(tmp_path):
    url = "/v1/projects/p1/locations/us-central1/publishers/google/models/m1:generateContent"
    ok = {
        "candidates": [{"content": {"parts": [{"text": '{"label": "b", "notes": []}'}]}}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5},
    }
    calls = []

    class Sess(requests.Session):
        def post(self, u, data=None, headers=None, timeout=None):
            body = json.loads(data)
            calls.append(body)
            r = requests.Response()
            if "thinkingConfig" in body["generationConfig"]:
                r.status_code, r._content = 400, b'{"error": "thinking_level is not supported"}'
            else:
                r.status_code, r._content = 200, json.dumps(ok).encode()
            return r

    g = GeminiVertexProvider("m1", project="p1", location="us-central1", session=Sess())
    assert g.url().endswith(url) and "us-central1-aiplatform" in g.url()
    out = LLMClient(g, prices={"m1": [0.1, 0.1]}, budget_usd=0.1).generate(req())
    assert out.data["label"] == "b" and len(calls) == 2
    LLMClient(g).generate(req("second"))
    assert "thinkingConfig" not in calls[-1]["generationConfig"]


def test_openai_compatible_body(tmp_path):
    reply = {
        "choices": [
            {"message": {"content": '{"label": "a", "notes": ["x"]}'}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        "model": "qwen2.5-7b-lora",
    }
    sess = ReplaySession(
        tmp_path, entries=[{"method": "POST", "path": "/v1/chat/completions", "body": reply}]
    )
    p = OpenAICompatibleProvider(
        "qwen2.5-7b-lora", base_url="http://127.0.0.1:8081/v1/", session=sess
    )
    res = LLMClient(p, budget_usd=0.01).generate(req(images=[llm.ImagePart(b"img")]))
    assert res.data["notes"] == ["x"] and res.cost_usd == 0.0  # a local model is free
    body = sess.recorded[0].body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert "Authorization" not in sess.recorded[0].headers
    with pytest.raises(llm.LLMConfigError):
        OpenAICompatibleProvider("m", base_url="")


class _Msg(SimpleNamespace):
    pass


def test_anthropic_provider_uses_structured_outputs_not_forced_tools():
    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)
            return _Msg(
                content=[_Msg(type="text", text='{"label": "a", "notes": []}')],
                usage=_Msg(input_tokens=50, output_tokens=12),
                stop_reason="end_turn",
                model="claude-haiku-5-5",
            )

    prov = AnthropicProvider(
        "claude-haiku-5-5", api_key="sk-ant-xyz1234567890", client=_Msg(messages=Messages())
    )
    res = LLMClient(prov, budget_usd=0.05).generate(req())
    assert res.data["label"] == "a"
    assert res.cost_usd == pytest.approx((50 * 0.10 + 12 * 0.50) / 1e6)
    fmt = seen["output_config"]["format"]
    assert fmt["type"] == "json_schema" and seen["output_config"]["effort"] == "low"
    assert fmt["schema"]["additionalProperties"] is False
    assert "maxLength" not in json.dumps(fmt["schema"]) and "maxItems" not in json.dumps(
        fmt["schema"]
    )
    assert "tool_choice" not in seen and "tools" not in seen and "temperature" not in seen

    class Refuses:
        def create(self, **kw):
            return _Msg(
                content=[], usage=_Msg(input_tokens=1, output_tokens=0), stop_reason="refusal"
            )

    with pytest.raises(LLMError, match="refusal"):
        LLMClient(AnthropicProvider(api_key="k" * 12, client=_Msg(messages=Refuses()))).generate(
            req()
        )

    class RateLimitError(Exception):
        status_code = 429

    class Limited:
        n = 0

        def create(self, **kw):
            Limited.n += 1
            raise RateLimitError("slow down")

    p = AnthropicProvider(api_key="k" * 12, client=_Msg(messages=Limited()))
    with pytest.raises(TransientLLMError):
        LLMClient(p, sleep=lambda s: None, max_retries=1).generate(req())
    assert Limited.n == 2


def test_schema_subsets():
    v = to_vertex_schema({"type": ["string", "null"], "maxLength": 5, "enum": ["x"]})
    assert v == {"type": "STRING", "nullable": True, "enum": ["x"]}
    a = to_anthropic_schema(
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}}
    )
    assert a == {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "additionalProperties": False,
        "required": ["n"],
    }


def test_settings_from_config_and_environment(tmp_path):
    s = llm.settings({"provider": "gemini", "budget_usd": "0.3"}, env={})
    assert s["model"] == "gemini-3.5-flash-lite" and s["budget_usd"] == 0.3
    assert s["_project"] is None
    with pytest.raises(llm.LLMConfigError, match="PULSE_LLM_PROJECT"):
        llm.make_provider(s)
    env = {
        "PULSE_LLM_PROVIDER": "openai",
        "PULSE_LLM_BASE_URL": "http://10.0.0.5:8081/v1",
        "PULSE_LLM_MODEL": "llama-8b-lora",
        "PULSE_LLM_API_KEY": "local-key-123",
    }
    s2 = llm.settings({"provider": "gemini"}, env=env)
    p = llm.make_provider(s2)
    assert isinstance(p, OpenAICompatibleProvider) and p.model == "llama-8b-lora"
    assert "local-key-123" not in repr(p)
    s3 = llm.settings({}, env={"GOOGLE_CLOUD_PROJECT": "proj-x", "PULSE_LLM_PROVIDER": "gemini"})
    assert s3["_project"] == "proj-x" and isinstance(llm.make_provider(s3), GeminiVertexProvider)
    assert llm.make_provider(llm.settings({}, env={})) is None
    with pytest.raises(llm.LLMConfigError):
        llm.settings({"provider": "litellm"}, env={})
    c = llm.client_from_settings(llm.settings({"provider": "fake"}, env={}), state_dir=tmp_path)
    assert c is not None and c.cache_dir == tmp_path / "llm-cache"
