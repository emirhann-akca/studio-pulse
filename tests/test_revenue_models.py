"""RevenueCat incremental export dedupe, proceeds/FX, LTV curve, AdMob eCPM."""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import D0, run_models

from studio_pulse import warehouse
from studio_pulse.connectors import revenuecat


def tx(tid: str, *, usd: float | None, renewal: int = 1, cur: str = "USD", local: float | None = None,
       tax: float | None = 0.0, commission: float | None = 0.15, updated: str = "2026-10-01T10:00:00",
       start: str = "2026-10-01T09:00:00", refunded: str | None = None, trial: bool = False, sandbox: bool = False,
       user: str = "u1") -> dict:
    return {
        "rc_original_app_user_id": user,
        "store_transaction_id": tid,
        "original_store_transaction_id": tid,
        "renewal_number": renewal,
        "product_identifier": "pro_monthly",
        "store": "play_store",
        "platform": "android",
        "country": "TR",
        "start_time": dt.datetime.fromisoformat(start),
        "is_trial_period": trial,
        "is_sandbox": sandbox,
        "price_in_usd": usd,
        "purchase_price_in_usd": usd,
        "purchased_currency": cur,
        "price_in_purchased_currency": local if local is not None else usd,
        "tax_percentage": tax,
        "commission_percentage": commission,
        "refunded_at": dt.datetime.fromisoformat(refunded) if refunded else None,
        "updated_at": dt.datetime.fromisoformat(updated),
        "first_seen_time": dt.datetime(2026, 10, 1, 8),
    }


def write(root: Path, folder: str, epoch: int, rows: list[dict]) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), d / f"transactions_{epoch}.parquet")


@pytest.fixture()
def rc(con, tmp_path):
    root = tmp_path / "rc"
    # Delivery 1 (Oct 1)
    write(root, "2026-10-01", 1790820000, [
        tx("GPA.1", usd=9.99, tax=0.20),                      # refunded later
        tx("GPA.2", usd=5.40, cur="EUR", local=5.00, tax=0.16),
        tx("GPA.3", usd=0.0, trial=True),                    # trial: 0 revenue
        tx("sub_1", usd=10.0, renewal=1, tax=0.0),           # same store id, two renewals
        tx("SANDBOX", usd=99.0, sandbox=True),               # must be ignored
    ])
    # Delivery 2 (Oct 5): GPA.1 refunded, GPA.2 re-sent unchanged, sub_1 renews,
    # and a TRY purchase arrives without a USD price (FX fallback 0.03).
    write(root, "2026-10-05", 1791165600, [
        tx("GPA.1", usd=0.0, tax=0.20, refunded="2026-10-05T08:00:00", updated="2026-10-05T08:00:00"),
        tx("GPA.2", usd=5.40, cur="EUR", local=5.00, tax=0.16),
        tx("sub_1", usd=10.0, renewal=2, tax=0.0, start="2026-10-05T09:00:00", updated="2026-10-05T09:00:00"),
        tx("GPA.4", usd=None, cur="TRY", local=300.0, tax=0.20, start="2026-10-05T10:00:00", updated="2026-10-05T10:00:00", user="u2"),
    ])
    warehouse.load_fx_rates(con, {"TRY": 0.03, "EUR": 1.08}, dt.date(2000, 1, 1))
    n = revenuecat.ingest(con, app_id="sub", source="revenuecat", path=str(root), log=lambda *_: None)
    assert n == 9  # every received version is stored
    con.execute("INSERT INTO meta_apps VALUES ('sub', 'Sub app', 'fitness', 'ANDROID', TRUE)")
    run_models(con)
    return con, root


def test_dedupe_keeps_latest_version(rc):
    con, _ = rc
    rows = {(r[0], r[1]): r[2:] for r in con.execute(
        "SELECT store_transaction_id, renewal_number, revenue_usd, proceeds_usd, is_refunded, versions_seen, usd_basis "
        "FROM stg_rc_transactions").fetchall()}
    assert set(rows) == {("GPA.1", 1), ("GPA.2", 1), ("GPA.3", 1), ("sub_1", 1), ("sub_1", 2), ("GPA.4", 1)}
    assert rows[("GPA.1", 1)][:3] == (0.0, 0.0, True)  # refund version wins
    assert rows[("GPA.2", 1)][3] == 2  # delivered twice, counted once
    assert rows[("GPA.4", 1)][0] == pytest.approx(9.0)  # 300 TRY * 0.03
    assert rows[("GPA.4", 1)][4] == "fx_table"


def test_naive_sum_would_double_count(rc):
    con, _ = rc
    naive = con.execute("SELECT SUM(price_in_usd) FROM raw_revenuecat_transactions WHERE NOT is_sandbox").fetchone()[0]
    deduped = con.execute("SELECT SUM(revenue_usd) FROM stg_rc_transactions").fetchone()[0]
    assert naive == pytest.approx(9.99 + 5.40 + 0 + 10 + 0 + 5.40 + 10)
    assert deduped == pytest.approx(5.40 + 10 + 10 + 9.0)


def test_proceeds_after_tax_and_commission(rc):
    con, _ = rc
    total = con.execute("SELECT SUM(proceeds_usd) FROM stg_rc_transactions").fetchone()[0]
    expected = 5.40 * (1 - 0.16 - 0.15) + 10 * (1 - 0.15) * 2 + 9.0 * (1 - 0.20 - 0.15)
    assert total == pytest.approx(expected)
    by_day = dict(con.execute("SELECT date, iap_revenue_usd FROM fct_app_daily WHERE app_id = 'sub'").fetchall())
    assert by_day[D0] == pytest.approx(5.40 * 0.69 + 10 * 0.85)
    assert by_day[D0 + dt.timedelta(days=4)] == pytest.approx(10 * 0.85 + 9.0 * 0.65)


def test_reingest_skips_known_files(rc):
    con, root = rc
    assert revenuecat.ingest(con, app_id="sub", source="revenuecat", path=str(root), log=lambda *_: None) == 0
    assert con.execute("SELECT COUNT(*) FROM raw_revenuecat_transactions").fetchone()[0] == 9


def test_csv_gz_export_with_missing_columns(con, tmp_path):
    """Older export versions have no tax/commission columns: they load as NULL."""
    d = tmp_path / "rc" / "2026-10-02"
    d.mkdir(parents=True)
    csv = (
        "store_transaction_id,renewal_number,price_in_usd,takehome_percentage,start_time,is_sandbox,purchased_currency\n"
        "T9,1,4.99,0.7,2026-10-02 10:00:00,false,USD\n"
    )
    (d / "transactions_1790906400.csv.gz").write_bytes(gzip.compress(csv.encode()))
    revenuecat.ingest(con, app_id="x", source="rc", path=str(tmp_path / "rc"), log=lambda *_: None)
    run_models(con)
    row = con.execute("SELECT revenue_usd, proceeds_usd FROM stg_rc_transactions").fetchone()
    assert row == (pytest.approx(4.99), pytest.approx(4.99 * 0.7))


def test_file_timestamp_parsing():
    assert revenuecat.file_timestamp("2026-10-01/transactions_1790820000.csv.gz") == dt.datetime(2026, 10, 1, 2, 0)
    assert revenuecat.file_timestamp("2026-10-01/transactions_1790820000123.parquet") == dt.datetime(2026, 10, 1, 2, 0, 0, 123000)
    assert revenuecat.file_timestamp("2026-10-01/part-0001.parquet") == dt.datetime(2026, 10, 1)
    assert revenuecat.file_timestamp("x/transactions_20261001T030000.parquet") == dt.datetime(2026, 10, 1, 3)


def test_ltv_curve_hand_computed(con):
    # 7 cohorts of 10 users; each cohort earns $1 of ads on day 0 and $2 of IAP on day 1.
    for c in range(7):
        cd = D0 + dt.timedelta(days=c)
        for n, users, iap, ad in ((0, 10, 0.0, 1.0), (1, 5, 2.0, 0.0)):
            con.execute(
                "INSERT INTO raw_ga4_cohort_days VALUES ('g', 'ANDROID', ?, ?, ?, 0, ?, ?, ?, 'ga4', now())",
                [cd, n, users, iap, ad, 1 if n == 1 else 0],
            )
    run_models(con)
    ltv = {r[0]: r[1:] for r in con.execute(
        "SELECT day_n, cohorts, cumulative_arpu_usd, iap_arpu_usd, ad_arpu_usd, payer_rate FROM fct_ltv_curve WHERE app_id = 'g'").fetchall()}
    # Last data day = Oct 7 + 1 = Oct 8, so day 0 has 7 complete cohorts and day 1 has 7; day 2 only 6 -> dropped.
    assert sorted(ltv) == [0, 1]
    assert ltv[0][1] == pytest.approx(1.0 / 10)
    assert ltv[1][1] == pytest.approx((1.0 + 2.0) / 10)
    assert ltv[1][2:] == (pytest.approx(0.2), pytest.approx(0.1), pytest.approx(0.1))


def test_admob_ecpm_and_ad_revenue(con):
    con.execute("INSERT INTO raw_admob_daily VALUES ('a', DATE '2026-10-01', 'ca-app-pub-x~1', 12.5, 'USD', 2500, 30, 3000, 2900, 'admob', now())")
    con.execute("INSERT INTO raw_admob_daily VALUES ('a', DATE '2026-10-01', 'ca-app-pub-x~2', 10.0, 'EUR', 1000, 10, 1100, 1050, 'admob', now())")
    warehouse.load_fx_rates(con, {"EUR": 1.1}, dt.date(2000, 1, 1))
    con.execute("INSERT INTO raw_ga4_active_users (app_id, platform, date, dau, sessions, history_days, _source) VALUES ('a', 'ANDROID', DATE '2026-10-01', 500, 600, 1, 'ga4')")
    run_models(con)
    r = con.execute("SELECT ad_revenue_usd, ad_impressions, ecpm_usd, arpdau, ad_basis FROM fct_app_daily WHERE app_id = 'a'").fetchone()
    assert r[0] == pytest.approx(12.5 + 11.0)
    assert r[1] == 3500
    assert r[2] == pytest.approx(23.5 / 3500 * 1000)
    assert r[3] == pytest.approx(23.5 / 500)
    assert r[4] == "admob_earnings"
