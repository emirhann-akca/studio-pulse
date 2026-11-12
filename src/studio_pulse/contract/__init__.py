"""Event contract v2: one YAML source (contract/pulse_v2.yaml), generated code everywhere.

`spec()` returns the normalized contract that `generate.py` wrote next to this
file (pulse_v2.json, shipped in the wheel); `validate` checks batches and events
against it. Constants for Python code are in `generated.py`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

SPEC_FILE = "pulse_v2.json"


@lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    text = resources.files("studio_pulse.contract").joinpath(SPEC_FILE).read_text(encoding="utf-8")
    return json.loads(text)


__all__ = ["spec", "SPEC_FILE"]
