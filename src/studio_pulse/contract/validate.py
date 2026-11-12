"""Validate v2 batches and events against the generated contract (pulse_v2.json).

The collector calls `check_envelope` once per request (a broken envelope fails
the whole request with HTTP 400 or 413) and `check_event` per event (a broken
event is quarantined with the reason, the rest of the batch is stored). Every
reason is a short stable code, e.g. ``missing:ts``, ``param_enum:setting``.

The rules are the same as in contract/generated/pulse_v2.batch.schema.json;
tests/test_contract_v2.py runs both over the same good and bad samples.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import spec as _spec

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Something that looks like an e-mail address anywhere in a string value.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class EnvelopeError(ValueError):
    """The request as a whole is unusable: HTTP `status` with `reason`."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Rules:
    spec: dict[str, Any]
    name_re: re.Pattern[str]
    reserved: frozenset[str]
    prefixes: tuple[str, ...]
    sdk_prefix: str
    enums: dict[str, frozenset[str]]


@lru_cache(maxsize=1)
def rules() -> Rules:
    s = _spec()
    n = s["names"]
    return Rules(
        spec=s,
        name_re=re.compile(n["pattern"]),
        reserved=frozenset(n["reserved_names"]),
        prefixes=tuple(n["reserved_prefixes"]),
        sdk_prefix=n["sdk_prefix"],
        enums={k: frozenset(v) for k, v in s["enums"].items()},
    )


def _value(key: str, v: Any, f: dict[str, Any], r: Rules) -> tuple[Any, str | None]:
    """(normalized value, None) or (None, reason) for one field spec."""
    if v is None:
        return (None, None) if f.get("nullable") else (None, f"null:{key}")
    t = f["type"]
    if t == "string":
        if not isinstance(v, str):
            return None, f"type:{key}"
        if "enum" in f and v not in r.enums[f["enum"]]:
            return None, f"enum:{key}"
        if f.get("format") == "uuid" and not UUID_RE.match(v):
            return None, f"uuid:{key}"
        if "max_len" in f and len(v) > f["max_len"]:
            return None, f"too_long:{key}"
        if "pattern" in f and not re.match(f["pattern"], v):
            return None, f"pattern:{key}"
        return v, None
    if t == "bool":
        return (v, None) if isinstance(v, bool) else (None, f"type:{key}")
    if t in ("int", "double"):
        if isinstance(v, bool) or not isinstance(v, int | float):
            return None, f"type:{key}"
        if isinstance(v, float) and not math.isfinite(v):
            return None, f"type:{key}"
        if t == "int":
            if isinstance(v, float):
                if not v.is_integer():
                    return None, f"type:{key}"
                v = int(v)
        if "min" in f and v < f["min"]:
            return None, f"range:{key}"
        if "max" in f and v > f["max"]:
            return None, f"range:{key}"
        return v, None
    if t == "object":
        return (v, None) if isinstance(v, dict) else (None, f"type:{key}")
    if t == "array":
        return (v, None) if isinstance(v, list) else (None, f"type:{key}")
    return None, f"type:{key}"


def _fields(obj: dict[str, Any], fields: dict[str, Any], r: Rules, prefix: str) -> str | None:
    for k, f in fields.items():
        if k not in obj:
            if f["required"]:
                return f"{prefix}missing:{k}"
            continue
        v, err = _value(k, obj[k], f, r)
        if err:
            return prefix + err
        obj[k] = v
    return None


def check_envelope(body: Any) -> tuple[int, str, dict[str, Any], list[Any]]:
    """(sent_at, schema, ctx, events) of a v2 batch, or EnvelopeError."""
    r = rules()
    s = r.spec
    if not isinstance(body, dict):
        raise EnvelopeError("not_object")
    if body.get("v") != s["batch_version"]:
        raise EnvelopeError("unsupported_version")
    batch = dict(body)
    err = _fields(batch, {k: f for k, f in s["batch"].items() if k != "v"}, r, "")
    if err:
        raise EnvelopeError(err)
    events = batch["events"]
    if not events:
        raise EnvelopeError("no_events")
    if len(events) > s["limits"]["batch_max_events"]:
        raise EnvelopeError("too_many_events", 413)
    ctx = dict(batch["ctx"])
    err = _fields(ctx, s["ctx"], r, "ctx.")
    if err:
        raise EnvelopeError(err)
    for k, v in ctx.items():
        if isinstance(v, str) and EMAIL_RE.search(v):
            raise EnvelopeError(f"ctx.pii_email:{k}")
    return batch["sent_at"], batch["schema"], ctx, events


def event_size(ev: Any) -> int:
    return len(json.dumps(ev, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def check_event(ev: Any, child: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """(normalized event, None) or (None, reason). Time checks are the collector's."""
    r = rules()
    s = r.spec
    if not isinstance(ev, dict):
        return None, "not_object"
    if event_size(ev) > s["limits"]["event_max_bytes"]:
        return None, "event_too_large"
    out = dict(ev)
    err = _fields(out, s["event"], r, "")
    if err:
        return None, err
    name = out["name"]
    if not r.name_re.match(name):
        return None, "bad_name"
    if name in r.reserved:
        return None, "reserved_name"
    if name.startswith(r.prefixes):
        return None, "reserved_prefix"
    catalog = s["events"].get(name)
    if catalog is None and name.startswith(r.sdk_prefix):
        return None, "unknown_sdk_event"
    params = dict(out.get("p") or {})
    if len(params) > s["limits"]["max_params"]:
        return None, "too_many_params"
    declared = catalog["params"] if catalog else {}
    for k, v in list(params.items()):
        if not isinstance(k, str) or not r.name_re.match(k) or k.startswith(r.prefixes):
            return None, f"bad_param_name:{k}"
        f = declared.get(k)
        if f is None:
            if v is None or isinstance(v, dict | list):
                return None, f"param_type:{k}"
            if isinstance(v, str) and len(v) > s["limits"]["string_max_len"]:
                return None, f"param_too_long:{k}"
            if isinstance(v, float) and not math.isfinite(v):
                return None, f"param_type:{k}"
        else:
            nv, perr = _value(k, v, f, r)
            if perr:
                return None, "param_" + perr
            params[k] = nv
        if isinstance(params[k], str) and EMAIL_RE.search(params[k]):
            return None, f"pii_email:{k}"
    for k, f in declared.items():
        if f["required"] and k not in params:
            return None, f"missing_param:{k}"
    uid = out.get("uid")
    if isinstance(uid, str) and EMAIL_RE.search(uid):
        return None, "pii_email:uid"
    if child:
        out["uid"] = None
        for k in list(params):
            f = declared.get(k)
            if (f and f.get("free_text")) or (f is None and isinstance(params[k], str)):
                del params[k]
    out["p"] = params
    return out, None


__all__ = ["EnvelopeError", "check_envelope", "check_event", "event_size", "rules"]
