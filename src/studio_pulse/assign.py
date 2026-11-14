"""Deterministic experiment assignment, the same in Python, Dart and C#.

The algorithm is GrowthBook's (MIT, https://github.com/growthbook/growthbook): an install
falls into a bucket in [0, 1) by hashing the experiment key with its install id (FNV-1a
32-bit over UTF-16 code units, hash version 2), and the bucket picks the variation whose
range contains it. Every Studio Pulse SDK implements it and is tested against GrowthBook's
own cases (contract/golden/assign_growthbook.json), so an assignment made on a phone can be
recomputed here from the install id: the warehouse checks the exposures for sample-ratio
mismatch and for installs that report a variation they could not have been given.
"""

from __future__ import annotations

FNV_OFFSET = 0x811C9DC5
FNV_PRIME = 0x01000193


def fnv1a32(text: str) -> int:
    h = FNV_OFFSET
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):  # one UTF-16 code unit at a time, like charCodeAt
        h = ((h ^ (data[i] | data[i + 1] << 8)) * FNV_PRIME) & 0xFFFFFFFF
    return h


def hash_value(seed: str, value: str, version: int = 2) -> float | None:
    """GrowthBook's hash: v2 = fnv(str(fnv(seed + value))) % 10000 / 10000; v1 kept for its cases."""
    if version == 2:
        return (fnv1a32(str(fnv1a32(seed + value))) % 10000) / 10000
    if version == 1:
        return (fnv1a32(value + seed) % 1000) / 1000
    return None


def equal_weights(n: int) -> list[float]:
    return [1 / n] * n if n > 0 else []


def bucket_ranges(
    n: int, coverage: float = 1.0, weights: list[float] | None = None
) -> list[tuple[float, float]]:
    coverage = min(max(coverage, 0.0), 1.0)
    equal = equal_weights(n)
    w = list(weights) if weights else equal
    if len(w) != n or not 0.99 <= sum(w) <= 1.01:
        w = equal
    out, cumulative = [], 0.0
    for x in w:
        start = cumulative
        cumulative += x
        out.append((start, start + coverage * x))
    return out


def choose_variation(bucket: float, ranges: list[tuple[float, float]]) -> int:
    for i, (lo, hi) in enumerate(ranges):
        if lo <= bucket < hi:
            return i
    return -1


def assign(
    experiment_key: str,
    install_id: str,
    variations: int = 2,
    weights: list[float] | None = None,
    coverage: float = 1.0,
) -> tuple[int, float]:
    """(variation or -1 when the install is outside the coverage, its bucket)."""
    bucket = hash_value(experiment_key, install_id, 2)
    assert bucket is not None
    return choose_variation(bucket, bucket_ranges(variations, coverage, weights)), bucket
