from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from studio_pulse import runner, warehouse
from studio_pulse.demo.export_files import flat_table, write_ga4_parquet

FIXTURES = Path(__file__).parent / "fixtures"
D0 = dt.date(2026, 10, 1)


@pytest.fixture()
def con():
    c = warehouse.connect(":memory:")
    yield c
    c.close()


def run_models(c) -> None:
    runner.run_models(c)


class GA4Builder:
    """Builds a tiny GA4-export-shaped Parquet file, one event per call."""

    PARAMS = {
        "ga_session_id": "int",
        "engagement_time_msec": "int",
        "fatal": "int",
        "level": "int",
        "success": "int",
        "value": "double",
        "tier": "string",
        "scene": "string",
        "kind": "string",
        "frames": "int",
        "ft_p50_ms": "double",
        "ft_p95_ms": "double",
        "slow_frames": "int",
        "mem_alloc_mb": "int",
        "duration_ms": "double",
        "median_fps": "double",
    }

    def __init__(self) -> None:
        self.cols: dict[str, list[Any]] = {}
        self.n = 0

    def add(
        self,
        day: int,
        name: str,
        user: str,
        *,
        platform: str = "ANDROID",
        first_touch_day: int | None = None,
        value_usd: float | None = None,
        version: str = "1.0.0",
        app: str = "com.example.app",
        ts_micros: int | None = None,
        **params: Any,
    ) -> GA4Builder:
        d = D0 + dt.timedelta(days=day)
        ft = D0 + dt.timedelta(days=first_touch_day if first_touch_day is not None else 0)
        base = {
            "event_date": d.strftime("%Y%m%d"),
            "event_timestamp": ts_micros
            if ts_micros is not None
            else int(dt.datetime(d.year, d.month, d.day, 12).timestamp() * 1e6) + self.n,
            "event_name": name,
            "user_pseudo_id": user,
            "user_first_touch_timestamp": int(
                dt.datetime(ft.year, ft.month, ft.day, 9).timestamp() * 1e6
            ),
            "platform": platform,
            "app_id": app,
            "app_version": version,
            "device_category": "mobile",
            "mobile_model_name": "Model X",
            "country": "Turkey",
            "event_value_in_usd": value_usd,
        }
        for k in params:
            if k not in self.PARAMS:
                raise KeyError(k)
        row = {**base, **{f"p_{k}": params.get(k) for k in self.PARAMS}}
        for k, v in row.items():
            self.cols.setdefault(k, [None] * self.n).append(v)
        self.n += 1
        return self

    def write(self, path: Path) -> Path:
        return write_ga4_parquet(flat_table(self.cols, self.PARAMS), path)
