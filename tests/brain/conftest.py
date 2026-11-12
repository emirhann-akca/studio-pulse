"""Fixtures for the brain tests: one full-scale demo warehouse per session.

The planted defects (every 7th level hard, version 1.8.0 of quartz_merge:
crashes, low-tier frame time, D1 drop among its installers) need the demo's
default scale: at 0.1 the cohort cells fall under the 50-user minimum.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from studio_pulse import alerts
from studio_pulse.config import load_config

ROOT = Path(__file__).resolve().parents[2]
AS_OF = dt.date(2026, 11, 1)


class BrainDemo:
    def __init__(self, root: Path):
        from studio_pulse import cli

        self.root = root
        rc = cli.main(
            ["demo", "--scale", "1.0", "--out", str(root / "site"), "--workdir", str(root / "work")]
        )
        assert rc == 0
        self.config_path = root / "work" / "pulse.yaml"
        self.cfg = load_config(self.config_path)
        self.warehouse = Path(self.cfg.warehouse)
        self.rules = alerts.load_rules(ROOT / "alerts.yaml")

    def copy(self, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "warehouse.duckdb"
        shutil.copyfile(self.warehouse, out)
        return out


@pytest.fixture(scope="session")
def brain_demo(tmp_path_factory) -> BrainDemo:
    return BrainDemo(tmp_path_factory.mktemp("brain_demo"))


@pytest.fixture(scope="session")
def demo_report(brain_demo):
    import duckdb

    from studio_pulse.brain import insights

    con = duckdb.connect(str(brain_demo.warehouse), read_only=True)
    try:
        return insights.run(con, as_of=AS_OF, rules=brain_demo.rules)
    finally:
        con.close()


@pytest.fixture()
def no_network(monkeypatch):
    """Any socket connection attempt fails the test (the brain never talks to a network)."""
    import socket

    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
