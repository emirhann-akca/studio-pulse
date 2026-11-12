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
        traffic: tuple[str, str, str] | None = None,
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
            "ts_source": traffic[0] if traffic else None,
            "ts_medium": traffic[1] if traffic else None,
            "ts_name": traffic[2] if traffic else None,
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


class DemoBuild:
    """A demo warehouse built once per test session (scale 0.1, ~3 s)."""

    def __init__(self, root: Path):
        from studio_pulse import cli

        self.root = root
        self.work = root / "work"
        self.site = root / "site"
        rc = cli.main(
            ["demo", "--scale", "0.1", "--out", str(self.site), "--workdir", str(self.work)]
        )
        assert rc == 0
        self.config_path = self.work / "pulse.yaml"

    def copy(self, dest: Path):
        """A private copy of the warehouse plus dest/pulse.yaml pointing at it.

        The copy's config reads the session's generated source files, so
        `pulse run` on it works, and writes its warehouse, state and site
        under dest.
        """
        import shutil

        import yaml

        from studio_pulse.config import load_config

        src_cfg = load_config(self.config_path)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_cfg.warehouse, dest / "warehouse.duckdb")
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        data.update(warehouse="warehouse.duckdb", state_dir="pulse-state", site_dir="site")
        for app in data["apps"]:
            for src in app["sources"].values():
                for key in ("path", "response_file", "replay"):
                    if key in src:
                        src[key] = str((self.work / src[key]).resolve())
        if (data.get("collector") or {}).get("data_dir"):  # the SDK games' collector store
            data["collector"]["data_dir"] = str(
                (self.work / data["collector"]["data_dir"]).resolve()
            )
        path = dest / "pulse.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return load_config(path)


@pytest.fixture(scope="session")
def demo_build(tmp_path_factory) -> DemoBuild:
    return DemoBuild(tmp_path_factory.mktemp("demo_build"))
