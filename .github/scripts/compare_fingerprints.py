"""Compare the model fingerprints uploaded by every test job (OS x Python).

Each job built the demo and exported every stg_/fct_ table as sorted CSV with a
SHA-256 (`pulse fingerprint`). When every hash agrees the tables are identical.
When a hash differs, the two CSVs are compared cell by cell: text, dates and
integer columns must be equal; float columns may differ by a relative 1e-9
(absolute 1e-12 near zero). A parallel SUM adds floats in a different order on
different machines (even between two Python versions on one OS), which moves
the last bits; rounding before hashing cannot hide that, because a value can sit
exactly on a rounding boundary. A real difference (a changed formula, a lost
row, a platform bug) is far larger than 1e-9 and still fails.

The result is published as annotations: a notice per job with the tables that
were identical and, for the others, the largest relative difference; an error
per table that differs beyond the tolerance, with the first differing cell.

    python compare_fingerprints.py <dir with one sub-directory per job>
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
from pathlib import Path

REL_TOL = 1e-9
ABS_TOL = 1e-12
_FLOATISH = re.compile(r"^-?(\d+\.\d*|\d*\.\d+|\d+(\.\d*)?[eE][-+]?\d+|nan|inf)$")


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    head = rows[0]
    # A NULL in a one-column table is an empty line, which csv reads as [].
    return head, [r if r else [""] * len(head) for r in rows[1:]]


def _num(cell: str) -> float:
    return float(cell) if cell not in ("", "nan") else math.nan


def compare_csv(
    a: str, b: str, rel_tol: float = REL_TOL, abs_tol: float = ABS_TOL
) -> tuple[bool, float, str]:
    """(equal within tolerance, max relative difference, first difference) of two table CSVs."""
    head_a, rows_a = _rows(a)
    head_b, rows_b = _rows(b)
    if head_a != head_b:
        return False, math.inf, f"columns differ: {head_a} vs {head_b}"
    if len(rows_a) != len(rows_b):
        return False, math.inf, f"row counts {len(rows_a)} vs {len(rows_b)}"
    ncol = len(head_a)
    # A float column is one with a decimal point, an exponent or nan/inf anywhere in it.
    floats = [any(_FLOATISH.match(r[i]) for r in rows_a + rows_b) for i in range(ncol)]

    def key(r: list[str]) -> tuple:
        exact = tuple(c for c, f in zip(r, floats, strict=True) if not f)
        approx = []
        for c, f in zip(r, floats, strict=True):
            if f:
                x = _num(c)
                approx.append(math.inf if math.isnan(x) else x)
        return exact + tuple(approx)

    rows_a.sort(key=key)
    rows_b.sort(key=key)
    worst = 0.0
    for n, (ra, rb) in enumerate(zip(rows_a, rows_b, strict=True)):
        for i, (x, y) in enumerate(zip(ra, rb, strict=True)):
            if x == y:
                continue
            where = f"row {n + 1}, column {head_a[i]}: {x!r} vs {y!r}"
            if not floats[i] or "" in (x, y):
                return False, math.inf, where
            fx, fy = _num(x), _num(y)
            if not (math.isfinite(fx) and math.isfinite(fy)):
                return False, math.inf, where
            diff = abs(fx - fy)
            scale = max(abs(fx), abs(fy))
            rel = diff / scale if scale else 0.0
            worst = max(worst, rel)
            if diff > max(abs_tol, rel_tol * scale):
                return False, rel, f"{where} (relative {rel:.2e})"
    return True, worst, ""


def main(root: Path) -> int:
    jobs = {}
    for fp in sorted(root.glob("*/fingerprint.json")):
        jobs[fp.parent.name.removeprefix("fingerprint-")] = json.loads(
            fp.read_text(encoding="utf-8")
        )
    if len(jobs) < 2:
        print(f"::error title=cross-OS model hash::only {len(jobs)} fingerprint(s) found in {root}")
        return 1
    combined = {j: d["sha256"] for j, d in jobs.items()}
    n_tables = len(next(iter(jobs.values()))["tables"])
    if len(set(combined.values())) == 1:
        sha = next(iter(combined.values()))
        print(
            f"::notice title=cross-OS model hash::{len(jobs)} jobs ({', '.join(jobs)}) "
            f"produced identical model tables ({n_tables} tables): sha256 {sha}"
        )
        return 0
    ref_job = sorted(jobs)[0]
    ref = jobs[ref_job]["tables"]
    failures = 0
    for job, data in sorted(jobs.items()):
        if job == ref_job:
            continue
        identical, tolerated = 0, []
        for table in sorted(set(ref) | set(data["tables"])):
            a, b = ref.get(table), data["tables"].get(table)
            if a == b:
                identical += 1
                continue
            if a is None or b is None:
                failures += 1
                print(f"::error title=model output differs ({job})::{table} missing in one job")
                continue
            ca = root / f"fingerprint-{ref_job}" / f"{table}.csv"
            cb = root / f"fingerprint-{job}" / f"{table}.csv"
            if not (ca.exists() and cb.exists()):
                failures += 1
                print(f"::error title=model output differs ({job})::{table}: CSV not uploaded")
                continue
            ok, worst, detail = compare_csv(
                ca.read_text(encoding="utf-8"), cb.read_text(encoding="utf-8")
            )
            if ok:
                tolerated.append(f"{table} (max rel {worst:.1e})")
            else:
                failures += 1
                print(
                    f"::error title=model output differs ({job})::"
                    + esc(f"{table} vs {ref_job}: {detail}")
                )
        summary = f"{identical} identical"
        if tolerated:
            summary += f"; equal within rel {REL_TOL:g}: " + ", ".join(tolerated)
        print(f"::notice title=cross-OS model hash ({job} vs {ref_job})::{summary}")
    if not failures:
        print(
            f"::notice title=cross-OS model hash::{len(jobs)} jobs, {n_tables} tables: equal "
            f"(text and integers exactly, floats within relative {REL_TOL:g})"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
