"""Compare the model fingerprints uploaded by every test job (OS x Python).

Each job built the demo and exported every stg_/fct_ table as sorted CSV with a
SHA-256 (`pulse fingerprint`). All jobs must agree. The result is published as
an annotation: a notice with the common hash, or one error per differing table
with the first differing CSV line.

    python compare_fingerprints.py <dir with one sub-directory per job>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


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
    if len(set(combined.values())) == 1:
        sha = next(iter(combined.values()))
        n = len(next(iter(jobs.values()))["tables"])
        print(
            f"::notice title=cross-OS model hash::{len(jobs)} jobs ({', '.join(jobs)}) "
            f"produced identical model tables ({n} tables): sha256 {sha}"
        )
        return 0
    ref_job = sorted(jobs)[0]
    ref = jobs[ref_job]["tables"]
    failures = 0
    for job, data in sorted(jobs.items()):
        for table in sorted(set(ref) | set(data["tables"])):
            a, b = ref.get(table), data["tables"].get(table)
            if a == b:
                continue
            failures += 1
            detail = f"{table}: {ref_job}={a} vs {job}={b}"
            ra, rb = (
                root / f"fingerprint-{ref_job}" / f"{table}.csv",
                root / f"fingerprint-{job}" / f"{table}.csv",
            )
            if ra.exists() and rb.exists():
                la = ra.read_text(encoding="utf-8").splitlines()
                lb = rb.read_text(encoding="utf-8").splitlines()
                for i, (x, y) in enumerate(zip(la, lb, strict=False)):
                    if x != y:
                        detail += f"\nfirst difference at line {i + 1}:\n{ref_job}: {x[:400]}\n{job}: {y[:400]}"
                        break
                else:
                    detail += f"\nrow counts {len(la)} vs {len(lb)}"
            print(f"::error title=model output differs ({job})::{esc(detail)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
