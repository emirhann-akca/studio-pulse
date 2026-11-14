"""Experiment assignment: GrowthBook's own cases (contract/golden), the same in every SDK."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio_pulse import assign

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "contract/golden/assign_growthbook.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("seed,value,version,expected", CASES["hash"])
def test_hash_matches_growthbook(seed, value, version, expected):
    got = assign.hash_value(seed, value, version)
    assert got == expected if expected is None else got == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("name,args,expected", CASES["getBucketRange"])
def test_bucket_ranges_match_growthbook(name, args, expected):
    n, coverage, weights = args
    got = assign.bucket_ranges(n, coverage, weights)
    assert len(got) == len(expected), name
    for (a, b), (x, y) in zip(got, expected, strict=True):
        assert a == pytest.approx(x, abs=1e-9) and b == pytest.approx(y, abs=1e-9), name


@pytest.mark.parametrize("name,bucket,ranges,expected", CASES["chooseVariation"])
def test_choose_variation_matches_growthbook(name, bucket, ranges, expected):
    assert assign.choose_variation(bucket, [tuple(r) for r in ranges]) == expected, name


@pytest.mark.parametrize("n,expected", CASES["getEqualWeights"])
def test_equal_weights_match_growthbook(n, expected):
    assert assign.equal_weights(n) == pytest.approx(expected)


def test_split_is_even_and_stable():
    """10,000 installs on a 50/50 test land within 2 points of half, and again the same way."""
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(10_000)]
    first = [assign.assign("booster_offer", i)[0] for i in ids]
    assert abs(first.count(0) / len(ids) - 0.5) < 0.02
    assert first == [assign.assign("booster_offer", i)[0] for i in ids]
    other = [assign.assign("another_test", i)[0] for i in ids]
    assert sum(a == b for a, b in zip(first, other, strict=True)) / len(ids) == pytest.approx(
        0.5, abs=0.03
    )
    half = [assign.assign("booster_offer", i, coverage=0.5)[0] for i in ids]
    assert abs(half.count(-1) / len(ids) - 0.5) < 0.02  # half the installs stay out
