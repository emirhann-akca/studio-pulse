"""Alert rules: thresholds, drop vs trailing baseline, filters, notifications."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from studio_pulse import alerts
from studio_pulse.alerts import Rule, RuleError

D = dt.date(2026, 10, 1)


@pytest.fixture()
def db():
    c = duckdb.connect()
    c.execute("CREATE TABLE fct_retention (app_id VARCHAR, cohort_date DATE, day_n INT, retention DOUBLE, cohort_size INT, is_complete BOOLEAN)")
    c.execute("CREATE TABLE fct_app_daily (app_id VARCHAR, date DATE, dau BIGINT, crash_free_users DOUBLE, arpdau DOUBLE)")
    c.execute("CREATE TABLE fct_perf_daily (app_id VARCHAR, date DATE, device_tier VARCHAR, ft_p50_ms DOUBLE, windows BIGINT)")
    # 14 baseline cohorts at 40% D1, then the newest at 30% (-25%).
    for i in range(15):
        c.execute("INSERT INTO fct_retention VALUES ('a', ?, 1, ?, 200, TRUE)", [D + dt.timedelta(days=i), 0.40 if i < 14 else 0.30])
        c.execute("INSERT INTO fct_retention VALUES ('b', ?, 1, 0.35, 200, TRUE)", [D + dt.timedelta(days=i)])
    c.execute("INSERT INTO fct_retention VALUES ('a', ?, 1, 0.01, 200, FALSE)", [D + dt.timedelta(days=15)])  # incomplete
    for i in range(3):
        c.execute("INSERT INTO fct_app_daily VALUES ('a', ?, 1000, ?, 0.05)", [D + dt.timedelta(days=i), [0.995, 0.993, 0.985][i]])
        c.execute("INSERT INTO fct_app_daily VALUES ('small', ?, 20, 0.90, 0.05)", [D + dt.timedelta(days=i)])
    c.execute("INSERT INTO fct_perf_daily VALUES ('a', ?, 'low', 22.5, 50), ('a', ?, 'high', 30.0, 50)", [D, D])
    return c


def rule(**kw) -> Rule:
    return Rule.from_dict(kw, {})


def test_drop_vs_baseline_fires_at_20_but_not_30(db):
    r20 = rule(name="d1", metric="d1_retention", condition="drop_vs_baseline", baseline_days=14, threshold_pct=20, min_sample=50)
    r30 = rule(name="d1b", metric="d1_retention", condition="drop_vs_baseline", baseline_days=14, threshold_pct=30, min_sample=50)
    fired = alerts.evaluate(db, [r20, r30])
    assert [(a.rule, a.app_id) for a in fired] == [("d1", "a")]
    a = fired[0]
    assert a.date == D + dt.timedelta(days=14)  # incomplete cohort ignored
    assert a.value == pytest.approx(0.30) and a.baseline == pytest.approx(0.40)
    assert "down 25.0%" in a.message


def test_below_threshold_with_min_sample(db):
    r = rule(name="crash", metric="crash_free_users", condition="below", threshold=0.99, min_sample=100, severity="critical")
    fired = alerts.evaluate(db, [r])
    assert [(a.app_id, a.severity) for a in fired] == [("a", "critical")]  # 'small' has 20 DAU < 100
    assert fired[0].value == pytest.approx(0.985)


def test_evaluate_days_averages_latest_points(db):
    r = rule(name="crash3", metric="crash_free_users", condition="below", threshold=0.99, evaluate_days=3, min_sample=100)
    assert alerts.evaluate(db, [r]) == []  # mean(0.995, 0.993, 0.985) = 0.991


def test_dimension_filter(db):
    r = rule(name="ft", metric="frame_time_p50_ms", where={"device_tier": "low"}, condition="above", threshold=20)
    fired = alerts.evaluate(db, [r])
    assert len(fired) == 1 and fired[0].value == pytest.approx(22.5)
    assert "[device_tier=low]" in fired[0].message


def test_missing_baseline_points_skip(db):
    r = rule(name="arp", metric="arpdau", condition="drop_vs_baseline", baseline_days=14, threshold_pct=10)
    assert alerts.evaluate(db, [r]) == []  # only 2 baseline days < 7 required


def test_min_z_ignores_drops_a_small_cohort_makes_by_chance():
    c = duckdb.connect()
    c.execute("CREATE TABLE fct_retention (app_id VARCHAR, cohort_date DATE, day_n INT, retention DOUBLE, cohort_size INT, is_complete BOOLEAN)")
    for app, size in (("small", 60), ("big", 2000)):
        for i in range(15):  # 35% baseline, newest cohort 26% (-26%)
            c.execute("INSERT INTO fct_retention VALUES (?, ?, 1, ?, ?, TRUE)", [app, D + dt.timedelta(days=i), 0.35 if i < 14 else 0.26, size])
    plain = rule(name="d1", metric="d1_retention", condition="drop_vs_baseline", threshold_pct=20, min_sample=50)
    guarded = rule(name="d1z", metric="d1_retention", condition="drop_vs_baseline", threshold_pct=20, min_sample=50, min_z=3)
    assert sorted(a.app_id for a in alerts.evaluate(c, [plain])) == ["big", "small"]
    fired = alerts.evaluate(c, [guarded])
    # 60 users: SE = sqrt(.35 * .65 / 60) = 6.2 points, the drop is 1.5 SE. 2000 users: 8.4 SE.
    assert [a.app_id for a in fired] == ["big"] and "z=-8.4" in fired[0].message
    with pytest.raises(RuleError):
        rule(name="x", metric="dau", condition="below", threshold=1, min_z=2)


def test_rule_validation():
    with pytest.raises(RuleError):
        rule(name="x", metric="nope", condition="above", threshold=1)
    with pytest.raises(RuleError):
        rule(name="x", metric="dau", condition="above")
    with pytest.raises(RuleError):
        rule(name="x", metric="dau", condition="drop_vs_baseline")
    with pytest.raises(RuleError):
        rule(name="x", metric="dau", condition="above", threshold=1, where={"device_tier": "low"})
    with pytest.raises(RuleError):
        rule(name="x", metric="dau", condition="above", threshold=1, color="red")


def test_shipped_rules_file_is_valid():
    from pathlib import Path

    rules = alerts.load_rules(Path(__file__).parents[1] / "alerts.yaml")
    names = {r.name for r in rules}
    assert {"d1_retention_drop", "crash_free_users_low", "low_tier_frame_time_high"} <= names


class FakeResp:
    status_code = 200


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeResp()


class FakeSMTP:
    sent = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def _alert():
    return alerts.Alert("crash", "a", "critical", "crash_free_users", D, 0.98, None, 0.99, "crash_free_users 98% is below 99%")


def test_notify_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setenv("PULSE_SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")
    monkeypatch.setenv("PULSE_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("PULSE_ALERT_EMAIL_TO", "team@example.invalid")
    s, logs = FakeSession(), []
    status = alerts.notify([_alert()], dry_run=True, session=s, smtp_factory=FakeSMTP, log=logs.append)
    assert status == {"slack": "dry-run", "email": "dry-run"}
    assert s.calls == [] and FakeSMTP.sent == []
    assert any("would POST" in line for line in logs)


def test_notify_sends_slack_and_email(monkeypatch):
    monkeypatch.setenv("PULSE_SLACK_WEBHOOK_URL", "https://hooks.example.invalid/x")
    monkeypatch.setenv("PULSE_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("PULSE_ALERT_EMAIL_TO", "team@example.invalid")
    s = FakeSession()
    status = alerts.notify([_alert()], session=s, smtp_factory=FakeSMTP, log=lambda *_: None)
    assert status == {"slack": "sent", "email": "sent"}
    assert s.calls[0][0] == "https://hooks.example.invalid/x"
    assert "crash" in s.calls[0][1]["text"]
    assert FakeSMTP.sent[-1]["To"] == "team@example.invalid"
