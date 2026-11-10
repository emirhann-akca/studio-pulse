"""A small dependency-aware runner for the SQL models in models/*.sql.

Each model file holds one SELECT statement. The model's name is the file name
without extension; the runner materialises it as a table (or a view, with a
`-- materialized: view` header). Dependencies are inferred by scanning the SQL
for the names of other models, so a model can simply `FROM stg_cohort_days`.
Models run in topological order (ties broken by file name). Every run uses
CREATE OR REPLACE, so running twice gives the same result.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import duckdb

_COMMENT_RE = re.compile(r"--[^\n]*")
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_HEADER_RE = re.compile(r"^--\s*(materialized|depends_on)\s*:\s*(.+)$", re.MULTILINE)


class ModelError(RuntimeError):
    pass


@dataclass
class Model:
    name: str
    path: Path
    sql: str
    materialized: str = "table"
    depends_on: set[str] = field(default_factory=set)
    description: str = ""


def default_models_dir() -> Path:
    repo_models = Path(__file__).resolve().parents[2] / "models"
    if repo_models.is_dir():
        return repo_models
    packaged = Path(str(resources.files("studio_pulse").joinpath("_models")))
    if packaged.is_dir():
        return packaged
    raise ModelError("could not locate the models directory")


def discover(models_dir: Path | None = None) -> list[Model]:
    models_dir = models_dir or default_models_dir()
    models: list[Model] = []
    for path in sorted(models_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        headers = dict((k, v.strip()) for k, v in _HEADER_RE.findall(sql))
        desc_lines = []
        for line in sql.splitlines():
            if not line.startswith("--"):
                break
            text = line[2:].strip()
            if not _HEADER_RE.match(line):
                desc_lines.append(text)
        models.append(
            Model(
                name=path.stem,
                path=path,
                sql=sql,
                materialized=headers.get("materialized", "table"),
                depends_on={
                    d.strip() for d in headers.get("depends_on", "").split(",") if d.strip()
                },
                description=" ".join(desc_lines).strip(),
            )
        )
    names = {m.name for m in models}
    for m in models:
        body = _STRING_RE.sub("''", _COMMENT_RE.sub("", m.sql))
        for other in names - {m.name}:
            if re.search(rf"\b{re.escape(other)}\b", body):
                m.depends_on.add(other)
        unknown = m.depends_on - names
        if unknown:
            raise ModelError(f"{m.name}: depends_on names unknown models {sorted(unknown)}")
    return models


def resolve_order(models: list[Model]) -> list[Model]:
    by_name = {m.name: m for m in models}
    remaining = {m.name: set(m.depends_on) for m in models}
    order: list[Model] = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            raise ModelError(f"dependency cycle among models: {sorted(remaining)}")
        for n in ready:
            order.append(by_name[n])
            del remaining[n]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


@dataclass
class ModelResult:
    name: str
    rows: int | None
    seconds: float


def run_models(
    con: duckdb.DuckDBPyConnection, models_dir: Path | None = None, verbose: bool = False
) -> list[ModelResult]:
    results = []
    for m in resolve_order(discover(models_dir)):
        body = m.sql.strip().rstrip(";")
        kind = "VIEW" if m.materialized == "view" else "TABLE"
        t0 = time.perf_counter()
        try:
            con.execute(f"CREATE OR REPLACE {kind} {m.name} AS\n{body}")
        except duckdb.Error as exc:
            raise ModelError(f"model {m.name} ({m.path.name}) failed: {exc}") from exc
        rows = con.execute(f"SELECT COUNT(*) FROM {m.name}").fetchone()[0]
        res = ModelResult(m.name, rows, time.perf_counter() - t0)
        results.append(res)
        if verbose:
            print(f"  model {m.name:<28} {rows:>9} rows  {res.seconds * 1000:7.1f} ms")
    return results
