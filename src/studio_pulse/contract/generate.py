"""Generate every contract artifact from contract/pulse_v2.yaml.

    python -m studio_pulse.contract.generate          # write all outputs
    python -m studio_pulse.contract.generate --check  # exit 1 if any output differs

Outputs (paths relative to the repository root) are listed in OUTPUTS. The
YAML itself is validated first: names follow GA4's rules and are not reserved,
automatic events and only those use the pulse_ prefix, enum references exist,
no event has more than max_params parameters. Text is written UTF-8 with LF.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path("contract/pulse_v2.yaml")
TYPES = ("string", "int", "double", "bool", "object", "array")
CATEGORIES = ("automatic", "game", "app")
UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
HEADER = "GENERATED from contract/pulse_v2.yaml by `python -m studio_pulse.contract.generate`. Do not edit."


class ContractError(ValueError):
    pass


# -- loading and validation -----------------------------------------------------------


def _field(name: str, raw: dict[str, Any], enums: dict[str, list[str]], max_len: int) -> dict:
    f = dict(raw or {})
    typ = f.get("type")
    if typ not in TYPES:
        raise ContractError(f"{name}: type must be one of {TYPES}, not {typ!r}")
    if "enum" in f and f["enum"] not in enums:
        raise ContractError(f"{name}: unknown enum {f['enum']!r}")
    out: dict[str, Any] = {"type": typ, "required": bool(f.get("required", False))}
    if typ == "string" and "enum" not in f and f.get("format") != "uuid":
        out["max_len"] = int(f.get("max_len", max_len))
    for k in ("enum", "format", "pattern", "min", "max"):
        if k in f:
            out[k] = f[k]
    out["nullable"] = bool(f.get("nullable", False))
    if f.get("free_text"):
        out["free_text"] = True
    if f.get("doc"):
        out["doc"] = str(f["doc"])
    unknown = set(f) - {"type", "required", "enum", "format", "pattern", "min", "max", "max_len"}
    unknown -= {"nullable", "free_text", "doc"}
    if unknown:
        raise ContractError(f"{name}: unknown keys {sorted(unknown)}")
    return out


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate the YAML and return the normalized spec (what pulse_v2.json holds)."""
    limits = {k: int(v) for k, v in raw["limits"].items()}
    names = raw["names"]
    pattern = re.compile(names["pattern"])
    reserved = set(names["reserved_names"])
    prefixes = tuple(names["reserved_prefixes"])
    sdk_prefix = names["sdk_prefix"]
    enums = {k: [str(x) for x in v] for k, v in raw["enums"].items()}
    for k, values in enums.items():
        if len(set(values)) != len(values):
            raise ContractError(f"enum {k}: duplicate values")
        for v in values:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
                raise ContractError(f"enum {k}: value {v!r} must be snake_case")
    for s in raw["production_stores"]:
        if s not in enums["install_store"]:
            raise ContractError(f"production store {s!r} is not an install_store")
    max_len = limits["string_max_len"]

    def section(key: str) -> dict[str, Any]:
        return {n: _field(f"{key}.{n}", f, enums, max_len) for n, f in raw[key].items()}

    events: dict[str, Any] = {}
    for name, ev in raw["events"].items():
        where = f"events.{name}"
        if not pattern.fullmatch(name) or len(name) > limits["name_max_len"]:
            raise ContractError(f"{where}: name breaks {names['pattern']}")
        if name in reserved or name.startswith(prefixes):
            raise ContractError(f"{where}: reserved by Firebase")
        cat = ev.get("category")
        if cat not in CATEGORIES:
            raise ContractError(f"{where}: category must be one of {CATEGORIES}")
        if (cat == "automatic") != name.startswith(sdk_prefix):
            raise ContractError(f"{where}: only automatic events use the {sdk_prefix} prefix")
        params = {}
        for pname, p in (ev.get("params") or {}).items():
            if not pattern.fullmatch(pname) or pname.startswith(prefixes):
                raise ContractError(f"{where}.{pname}: parameter name breaks the rules")
            f = _field(f"{where}.{pname}", p, enums, max_len)
            if f["type"] in ("object", "array"):
                raise ContractError(f"{where}.{pname}: params are scalars")
            params[pname] = f
        if len(params) > limits["max_params"]:
            raise ContractError(f"{where}: more than {limits['max_params']} params")
        events[name] = {
            "category": cat,
            "doc": str(ev.get("doc", "")),
            "firebase": bool(ev.get("firebase", True)),
            "params": params,
        }
    storage = {}
    for table, t in raw["storage"].items():
        cols = []
        for c in t["columns"]:
            col = {"name": c["name"], "type": c["type"], "mode": c.get("mode", "NULLABLE")}
            if c.get("doc"):
                col["doc"] = c["doc"]
            cols.append(col)
        storage[table] = {"doc": t.get("doc", ""), "columns": cols}
    return {
        "contract": raw["contract"],
        "version": str(raw["version"]),
        "batch_version": int(raw["batch_version"]),
        "limits": limits,
        "names": {
            "pattern": names["pattern"],
            "sdk_prefix": sdk_prefix,
            "reserved_prefixes": list(prefixes),
            "reserved_names": sorted(reserved),
        },
        "enums": enums,
        "production_stores": list(raw["production_stores"]),
        "batch": section("batch"),
        "ctx": section("ctx"),
        "event": section("event"),
        "events": events,
        "storage": storage,
        "privacy": [
            {k: str(item[k]) for k in ("data", "apple", "play", "from")} for item in raw["privacy"]
        ],
    }


def load(path: Path | None = None) -> dict[str, Any]:
    p = path or ROOT / SOURCE
    return normalize(yaml.safe_load(p.read_text(encoding="utf-8")))


# -- identifiers ----------------------------------------------------------------------

DART_RESERVED = {
    "assert", "break", "case", "catch", "class", "const", "continue", "default", "do", "else",
    "enum", "extends", "false", "final", "finally", "for", "if", "in", "is", "new", "null",
    "rethrow", "return", "super", "switch", "this", "throw", "true", "try", "var", "void",
    "while", "with",
}  # fmt: skip


def pascal(s: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in s.split("_") if part)


def camel(s: str) -> str:
    p = pascal(s)
    out = p[:1].lower() + p[1:]
    return out + "Value" if out in DART_RESERVED else out


def upper(s: str) -> str:
    return s.upper()


def all_params(spec: dict[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for ev in spec["events"].values():
        for p in ev["params"]:
            seen.setdefault(p, None)
    return list(seen)


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


# -- Python -----------------------------------------------------------------------------


def render_python(spec: dict[str, Any]) -> str:
    lines = [
        f'"""{HEADER}',
        "",
        "Names, enum values and limits of event contract v2 (docs/CONTRACT.md).",
        '"""',
        "",
    ]

    def tuple_(name: str, values: list[str]) -> None:
        if len(values) < 2:  # the formatter keeps (x,) and () on one line
            lines.append(f"{name} = ({''.join(_q(v) + ',' for v in values)})")
            return
        lines.append(f"{name} = (")
        lines.extend(f"    {_q(v)}," for v in values)
        lines.append(")")

    lines += [
        f"CONTRACT_VERSION = {_q(spec['version'])}",
        f"BATCH_VERSION = {spec['batch_version']}",
        "",
    ]
    lines.append("# Limits")
    lines += [f"LIMIT_{upper(k)} = {v}" for k, v in spec["limits"].items()]
    lines += ["", "# Name rules", f"NAME_PATTERN = {_q(spec['names']['pattern'])}"]
    lines.append(f"SDK_PREFIX = {_q(spec['names']['sdk_prefix'])}")
    tuple_("RESERVED_PREFIXES", spec["names"]["reserved_prefixes"])
    tuple_("RESERVED_NAMES", spec["names"]["reserved_names"])
    lines += ["", "# Events"]
    lines += [f"EVENT_{upper(n)} = {_q(n)}" for n in spec["events"]]
    for cat in CATEGORIES:
        tuple_(
            f"{upper(cat)}_EVENTS", [n for n, e in spec["events"].items() if e["category"] == cat]
        )
    lines += ["", "# Event parameters"]
    lines += [f"PARAM_{upper(p)} = {_q(p)}" for p in all_params(spec)]
    lines += ["", "# Context (ctx) and event fields"]
    lines += [f"CTX_{upper(k)} = {_q(k)}" for k in spec["ctx"]]
    lines += [f"FIELD_{upper(k)} = {_q(k)}" for k in spec["event"]]
    lines += ["", "# Enum values"]
    for enum, values in spec["enums"].items():
        lines += [f"{upper(enum)}_{upper(v)} = {_q(v)}" for v in values]
        tuple_(f"ENUM_{upper(enum)}", values)
    tuple_("PRODUCTION_STORES", spec["production_stores"])
    return "\n".join(lines) + "\n"


# -- C# ---------------------------------------------------------------------------------


def _cs_const(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"public const bool {name} = {'true' if value else 'false'};"
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return f"public const int {name} = {value};"
        return f"public const long {name} = {value}L;"
    return f"public const string {name} = {_q(value)};"


def render_csharp(spec: dict[str, Any]) -> str:
    out = [
        f"// <auto-generated>{HEADER}</auto-generated>",
        "// Event contract v2 for the Unity SDK (docs/CONTRACT.md). Pure C#: no UnityEngine use.",
        "namespace Studio.Pulse",
        "{",
        "    public static class PulseContract",
        "    {",
        "        " + _cs_const("Version", spec["version"]),
        "        " + _cs_const("BatchVersion", spec["batch_version"]),
    ]

    def cls(name: str, members: list[str], doc: str = "") -> None:
        out.append("")
        if doc:
            out.append(f"        /// <summary>{doc}</summary>")
        out.append(f"        public static class {name}")
        out.append("        {")
        out.extend(f"            {m}" for m in members)
        out.append("        }")

    def arr(name: str, values: list[str]) -> str:
        return f"public static readonly string[] {name} = {{ {', '.join(_q(v) for v in values)} }};"

    cls("Limits", [_cs_const(pascal(k), v) for k, v in spec["limits"].items()])
    names = spec["names"]
    cls(
        "Names",
        [
            _cs_const("Pattern", names["pattern"]),
            _cs_const("SdkPrefix", names["sdk_prefix"]),
            arr("ReservedPrefixes", names["reserved_prefixes"]),
            arr("ReservedNames", names["reserved_names"]),
        ],
        "GA4-compatible name rules; the Firebase sink drops nothing that passes them.",
    )
    cls("Events", [_cs_const(pascal(n), n) for n in spec["events"]])
    cls("Params", [_cs_const(pascal(p), p) for p in all_params(spec)])
    cls("Ctx", [_cs_const(pascal(k), k) for k in spec["ctx"]], "Batch context keys.")
    cls("Fields", [_cs_const(pascal(k), k) for k in spec["event"]], "Event keys.")
    for enum, values in spec["enums"].items():
        cls(pascal(enum), [_cs_const(pascal(v), v) for v in values] + [arr("All", values)])
    cls("Stores", [arr("Production", spec["production_stores"])])
    out += ["    }", "}"]
    return "\n".join(out) + "\n"


# -- Dart -------------------------------------------------------------------------------


def _dart_const(name: str, value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"static const int {name} = {value};"
    return f"static const String {name} = {_dart_str(value)};"


def _dart_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$") + "'"


def render_dart(spec: dict[str, Any]) -> str:
    out = [f"// {HEADER}", "//", "// Event contract v2 for the Flutter SDK (docs/CONTRACT.md)."]

    def cls(name: str, members: list[str], doc: str) -> None:
        out.append("")
        out.append(f"/// {doc}")
        out.append(f"class {name} {{")
        out.append(f"  {name}._();")
        out.append("")
        out.extend(f"  {m}" for m in members)
        out.append("}")

    def lst(name: str, values: list[str]) -> str:
        return f"static const List<String> {name} = <String>[{', '.join(_dart_str(v) for v in values)}];"

    cls(
        "PulseContract",
        [
            _dart_const("version", spec["version"]),
            _dart_const("batchVersion", spec["batch_version"]),
        ],
        "Contract and envelope versions.",
    )
    cls("PulseLimits", [_dart_const(camel(k), v) for k, v in spec["limits"].items()], "Limits.")
    names = spec["names"]
    cls(
        "PulseNames",
        [
            _dart_const("pattern", names["pattern"]),
            _dart_const("sdkPrefix", names["sdk_prefix"]),
            lst("reservedPrefixes", names["reserved_prefixes"]),
            lst("reservedNames", names["reserved_names"]),
        ],
        "GA4-compatible name rules.",
    )
    cls("PulseEvents", [_dart_const(camel(n), n) for n in spec["events"]], "Event names.")
    cls("PulseParams", [_dart_const(camel(p), p) for p in all_params(spec)], "Parameter names.")
    cls("PulseCtx", [_dart_const(camel(k), k) for k in spec["ctx"]], "Batch context keys.")
    cls("PulseFields", [_dart_const(camel(k), k) for k in spec["event"]], "Event keys.")
    for enum, values in spec["enums"].items():
        cls(
            "Pulse" + pascal(enum),
            [_dart_const(camel(v), v) for v in values] + [lst("all", values)],
            f"Values of `{enum}`.",
        )
    cls(
        "PulseStores",
        [lst("production", spec["production_stores"])],
        "Stores that count as production.",
    )
    return "\n".join(out) + "\n"


# -- JSON Schema ------------------------------------------------------------------------

_JSON_TYPE = {"string": "string", "int": "integer", "double": "number", "bool": "boolean"}


def _schema_of(f: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if f["type"] in _JSON_TYPE:
        t: Any = _JSON_TYPE[f["type"]]
        s["type"] = [t, "null"] if f.get("nullable") else t
    elif f["type"] == "object":
        s["type"] = "object"
    if "enum" in f:
        s["enum"] = list(spec["enums"][f["enum"]]) + ([None] if f.get("nullable") else [])
    if "max_len" in f:
        s["maxLength"] = f["max_len"]
    if f.get("format") == "uuid":
        s["pattern"] = UUID_PATTERN
    elif "pattern" in f:
        s["pattern"] = f["pattern"]
    if "min" in f:
        s["minimum"] = f["min"]
    if "max" in f:
        s["maximum"] = f["max"]
    if f.get("doc"):
        s["description"] = f["doc"]
    return s


def _object(fields: dict[str, Any], spec: dict[str, Any], extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": [k for k, f in fields.items() if f["required"]],
        "properties": {
            k: _schema_of(f, spec) for k, f in fields.items() if f["type"] not in ("array",)
        },
        "additionalProperties": extra,
    }


def render_json_schema(spec: dict[str, Any]) -> str:
    lim, names = spec["limits"], spec["names"]
    scalar = {
        "anyOf": [
            {"type": "string", "maxLength": lim["string_max_len"]},
            {"type": "number"},
            {"type": "boolean"},
        ]
    }
    defs: dict[str, Any] = {"scalar": scalar}
    ctx = _object(spec["ctx"], spec, True)
    defs["ctx"] = ctx
    params_generic = {
        "type": "object",
        "maxProperties": lim["max_params"],
        "propertyNames": {"pattern": names["pattern"]},
        "additionalProperties": {"$ref": "#/$defs/scalar"},
    }
    defs["params"] = params_generic
    prefix_alt = "|".join(re.escape(p) for p in names["reserved_prefixes"])
    event = _object({k: v for k, v in spec["event"].items() if k != "p"}, spec, True)
    event["properties"]["name"] = {
        "type": "string",
        "pattern": names["pattern"],
        "not": {
            "anyOf": [{"enum": names["reserved_names"]}, {"pattern": f"^({prefix_alt})"}],
        },
    }
    event["properties"]["p"] = {"$ref": "#/$defs/params"}
    branches = []
    for name, ev in spec["events"].items():
        key = f"p_{name}"
        p = _object(ev["params"], spec, {"$ref": "#/$defs/scalar"})
        p["maxProperties"] = lim["max_params"]
        defs[key] = p
        then: dict[str, Any] = {"properties": {"p": {"$ref": f"#/$defs/{key}"}}}
        if p["required"]:
            then["required"] = ["p"]
        branches.append({"if": {"properties": {"name": {"const": name}}}, "then": then})
    sdk = [n for n, e in spec["events"].items() if e["category"] == "automatic"]
    branches.append(
        {
            "if": {"properties": {"name": {"pattern": f"^{re.escape(names['sdk_prefix'])}"}}},
            "then": {"properties": {"name": {"enum": sdk}}},
        }
    )
    event["allOf"] = branches
    defs["event"] = event
    batch = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/emirhann-akca/studio-pulse/blob/main/contract/generated/pulse_v2.batch.schema.json",
        "title": f"Studio Pulse batch v{spec['batch_version']} (contract {spec['version']})",
        "description": f"{HEADER} Events also must be at most {lim['event_max_bytes']} bytes as compact JSON and the body at most {lim['batch_max_bytes']} bytes; those limits are checked by the collector.",
        "type": "object",
        "required": [k for k, f in spec["batch"].items() if f["required"]],
        "properties": {
            "v": {"const": spec["batch_version"]},
            "schema": _schema_of(spec["batch"]["schema"], spec),
            "sent_at": _schema_of(spec["batch"]["sent_at"], spec),
            "ctx": {"$ref": "#/$defs/ctx"},
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": lim["batch_max_events"],
                "items": {"$ref": "#/$defs/event"},
            },
        },
        "$defs": defs,
    }
    return json.dumps(batch, indent=2, ensure_ascii=False) + "\n"


# -- BigQuery ---------------------------------------------------------------------------


def render_bigquery(spec: dict[str, Any], table: str) -> str:
    cols = [
        {
            k: v
            for k, v in (
                ("name", c["name"]),
                ("type", c["type"]),
                ("mode", c["mode"]),
                ("description", c.get("doc")),
            )
            if v
        }
        for c in spec["storage"][table]["columns"]
    ]
    return json.dumps(cols, indent=2, ensure_ascii=False) + "\n"


# -- docs -------------------------------------------------------------------------------


def _md(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _field_rows(fields: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    rows = ["| Key | Type | Required | Rule | Meaning |", "|---|---|---|---|---|"]
    for k, f in fields.items():
        rule = []
        if "enum" in f:
            rule.append(f"one of `{f['enum']}`")
        if "max_len" in f:
            rule.append(f"<= {f['max_len']} chars")
        if f.get("format") == "uuid":
            rule.append("UUID")
        if "min" in f:
            rule.append(f">= {f['min']}")
        if "max" in f:
            rule.append(f"<= {f['max']}")
        if f.get("nullable"):
            rule.append("may be null")
        if f.get("free_text"):
            rule.append("free text (dropped in child mode)")
        req = "yes" if f["required"] else ""
        rows.append(
            f"| `{k}` | {f['type']} | {req} | {_md(', '.join(rule))} | {_md(f.get('doc', ''))} |"
        )
    return rows


def render_docs(spec: dict[str, Any]) -> str:
    lim, names = spec["limits"], spec["names"]
    out = [
        "# Event contract v2",
        "",
        f"<!-- {HEADER} -->",
        "",
        "One YAML file, [`contract/pulse_v2.yaml`](../contract/pulse_v2.yaml), defines every",
        "event, parameter, enum value and limit. The generator writes the code every party",
        "uses, and CI fails when a regenerated file differs from the committed one:",
        "",
        "| Output | Used by |",
        "|---|---|",
    ]
    for path, (who, _) in OUTPUTS.items():
        out.append(f"| `{path}` | {who} |")
    out += [
        "",
        "Contract v1 (`pulse_perf` / `pulse_load` as JSON lines, [EVENTS.md](EVENTS.md)) stays",
        "valid: the collector accepts v1 lines on `POST /v1/ndjson` and upgrades them.",
        "",
        "## Rules",
        "",
        f"- **Names**: `{names['pattern']}` (GA4: 1-{lim['name_max_len']} chars), at most {lim['max_params']} parameters per event,",
        f"  string values at most {lim['string_max_len']} characters unless a parameter says otherwise.",
        f"- **Reserved**: names Firebase drops are refused ({', '.join(f'`{n}`' for n in names['reserved_names'])}),",
        f"  as are the prefixes {', '.join(f'`{p}`' for p in names['reserved_prefixes'])}.",
        f"- **`{names['sdk_prefix']}` prefix**: only the SDK's automatic events use it; an unknown `{names['sdk_prefix']}*` name is refused.",
        "- **Custom events**: any other valid name, with scalar parameters (string, number, bool). Keep the set small and static.",
        "- **Ids**: `id` is a UUIDv7 made on the device, `seq` a per-install counter kept on disk.",
        "  The collector and the warehouse drop repeats of `id` and of (`install_id`, `seq`); gaps in `seq` measure loss.",
        "- **Time**: `ts` and `sent_at` are device-clock milliseconds. The collector stores",
        "  `ts = received_at - (sent_at - ts_client)` (Segment's skew correction) next to the raw value,",
        f"  quarantines events more than {lim['future_tolerance_ms'] // 60000} min in the future after correction",
        f"  or before {lim['min_ts_ms']} ms (2020), and flags events older than {lim['late_after_ms'] // 86400000} days as `late`.",
        f"- **Sizes**: at most {lim['batch_max_events']} events and {lim['batch_max_bytes']:,} bytes per batch (uncompressed and",
        f"  inflated), at most {lim['event_max_bytes']:,} bytes per event as compact JSON. Too large -> HTTP 413 (the SDK halves the batch);",
        "  an oversized event is quarantined.",
        "- **Privacy**: random `install_id` only; never IDFA, AAID, IDFV, Android ID, e-mail, name or IP.",
        "  String values that look like an e-mail address are quarantined. `consent: denied` batches are",
        "  not stored; `child: true` strips `uid` and free-text parameters. See [PRIVACY.md](PRIVACY.md).",
        f"- **SDK behaviour** (limits below): flush at {lim['flush_events']} events / {lim['flush_interval_ms'] // 1000} s / background / quit,",
        f"  session timeout {lim['session_timeout_ms'] // 60000} min in the background, retry with backoff",
        f"  {lim['retry_min_ms'] // 1000}-{lim['retry_max_ms'] // 1000} s plus jitter on network errors, 408, 429 and 5xx; drop on 400; disable until the next launch on 401/403.",
        "",
        "## Batch envelope (`POST /v2/batch`)",
        "",
        "Headers: `Content-Type: application/json`, optional `Content-Encoding: gzip`, `X-Pulse-Key: <public write key>`.",
        "",
        *_field_rows(spec["batch"], spec),
        "",
        "### `ctx`",
        "",
        *_field_rows(spec["ctx"], spec),
        "",
        "### Event",
        "",
        *_field_rows(spec["event"], spec),
        "",
        "## Event catalog",
        "",
    ]
    for cat, title in (
        ("automatic", "Automatic (the SDK sends these)"),
        ("game", "Game"),
        ("app", "App"),
    ):
        out += [f"### {title}", ""]
        for name, ev in spec["events"].items():
            if ev["category"] != cat:
                continue
            fb = "" if ev["firebase"] else " Not forwarded to Firebase."
            out += [f"#### `{name}`", "", _md(ev["doc"]) + fb, ""]
            if ev["params"]:
                out += _field_rows(ev["params"], spec) + [""]
    out += ["## Enums", "", "| Enum | Values |", "|---|---|"]
    for enum, values in spec["enums"].items():
        out.append(f"| `{enum}` | {', '.join(f'`{v}`' for v in values)} |")
    out += [
        "",
        f"Production stores (count towards a game being live): {', '.join(f'`{s}`' for s in spec['production_stores'])}.",
        "",
        "## Limits",
        "",
        "| Limit | Value |",
        "|---|---|",
    ]
    out += [f"| `{k}` | {v} |" for k, v in lim.items()]
    out += [
        "",
        "## Privacy mapping",
        "",
        "What the SDK collects, for Apple's privacy manifest and App Store labels and Google Play's",
        "Data safety form ([PRIVACY.md](PRIVACY.md); legal review needed before a store submission).",
        "",
        "| Data | Apple (`NSPrivacyCollectedDataType`) | Google Play Data safety | Comes from |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {_md(p['data'])} | `{p['apple'].replace('NSPrivacyCollectedDataType', '')}` | {_md(p['play'])} | {_md(p['from'])} |"
        for p in spec["privacy"]
    ]
    out += ["", "## Stored tables", ""]
    for table, t in spec["storage"].items():
        out += [
            f"### `{table}`",
            "",
            _md(t["doc"]),
            "",
            "| Column | Type | Mode | Meaning |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{c['name']}` | {c['type']} | {c['mode']} | {_md(c.get('doc', ''))} |"
            for c in t["columns"]
        ]
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_xcprivacy(spec: dict[str, Any]) -> str:
    """Apple privacy manifest for the SDK's iOS plug-in: data types from `privacy`.

    No tracking, no tracking domains; every type linked to the user (Apple treats
    pseudonymous analytics as linked unless strong protections exist) and used for
    Analytics and App Functionality. No required-reason API is declared: the
    plug-in itself uses none (Unity declares PlayerPrefs / UserDefaults for the
    engine); add them here if the native code starts using one.
    """
    import plistlib

    manifest = {
        "NSPrivacyTracking": False,
        "NSPrivacyTrackingDomains": [],
        "NSPrivacyCollectedDataTypes": [
            {
                "NSPrivacyCollectedDataType": p["apple"],
                "NSPrivacyCollectedDataTypeLinked": True,
                "NSPrivacyCollectedDataTypeTracking": False,
                "NSPrivacyCollectedDataTypePurposes": [
                    "NSPrivacyCollectedDataTypePurposeAnalytics",
                    "NSPrivacyCollectedDataTypePurposeAppFunctionality",
                ],
            }
            for p in spec["privacy"]
        ],
        "NSPrivacyAccessedAPITypes": [],
    }
    return plistlib.dumps(manifest, sort_keys=False).decode("utf-8")


def render_spec_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=1, ensure_ascii=False) + "\n"


OUTPUTS: dict[str, tuple[str, Callable[[dict[str, Any]], str]]] = {
    "src/studio_pulse/contract/pulse_v2.json": ("collector validation (shipped in the wheel)", render_spec_json),
    "src/studio_pulse/contract/generated.py": ("Python code (`from studio_pulse.contract import generated as C`)", render_python),
    "unity/com.studio.pulse/Runtime/Core/PulseContract.g.cs": ("Unity SDK (`Studio.Pulse.PulseContract`)", render_csharp),
    "flutter/studio_pulse/lib/src/contract.g.dart": ("Flutter SDK (`PulseEvents`, `PulseParams` ...)", render_dart),
    "contract/generated/pulse_v2.batch.schema.json": ("JSON Schema of a batch (any language)", render_json_schema),
    "contract/generated/bigquery/events.json": ("BigQuery `pulse_raw.events`", lambda s: render_bigquery(s, "events")),
    "contract/generated/bigquery/quarantine.json": ("BigQuery `pulse_raw.quarantine`", lambda s: render_bigquery(s, "quarantine")),
    "contract/generated/bigquery/app_registry.json": ("BigQuery `pulse_raw.app_registry`", lambda s: render_bigquery(s, "app_registry")),
    "contract/generated/bigquery/deletion_requests.json": ("BigQuery `pulse_raw.deletion_requests`", lambda s: render_bigquery(s, "deletion_requests")),
    "contract/generated/PrivacyInfo.xcprivacy": ("iOS plug-in privacy manifest (copy into the plug-in)", render_xcprivacy),
    "docs/CONTRACT.md": ("people (this page)", render_docs),
}  # fmt: skip


def render_all(spec: dict[str, Any] | None = None) -> dict[str, str]:
    spec = spec or load()
    return {path: fn(spec) for path, (_, fn) in OUTPUTS.items()}


def stale(root: Path = ROOT) -> list[str]:
    """Outputs whose committed text differs from what the YAML generates."""
    out = []
    for rel, text in render_all(load(root / SOURCE)).items():
        p = root / rel
        if not p.exists() or p.read_bytes() != text.encode("utf-8"):
            out.append(rel)
    return out


def write_all(root: Path = ROOT) -> list[str]:
    written = []
    for rel, text in render_all(load(root / SOURCE)).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)
            written.append(rel)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m studio_pulse.contract.generate")
    ap.add_argument("--check", action="store_true", help="exit 1 when an output is out of date")
    ap.add_argument("--root", default=str(ROOT), help="repository root")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / SOURCE).exists():
        print(f"{SOURCE} not found under {root} (run from a repository checkout)", file=sys.stderr)
        return 2
    try:
        if args.check:
            bad = stale(root)
            for rel in bad:
                print(f"out of date: {rel}", file=sys.stderr)
            if bad:
                print("run: python -m studio_pulse.contract.generate", file=sys.stderr)
            return 1 if bad else 0
        for rel in write_all(root):
            print(f"wrote {rel}")
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
