"""SQL dialects for source-side extraction queries.

The GA4 and Crashlytics extraction SQL is written once and rendered for either
BigQuery (production) or DuckDB (local GA4-export-shaped files used by the demo
and the tests). Only the handful of expressions that differ between the two
engines live here.
"""

from __future__ import annotations

import datetime as dt
import re

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def check_ident(value: str, what: str = "identifier") -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def check_key(value: str) -> str:
    if not _KEY_RE.match(value or ""):
        raise ValueError(f"invalid event/param name: {value!r}")
    return value


def sql_list(values: list[str] | tuple[str, ...]) -> str:
    return ", ".join(f"'{check_key(v)}'" for v in values) or "''"


class Dialect:
    name = "abstract"

    # -- tables -------------------------------------------------------------
    def ga4_events(self) -> str:
        raise NotImplementedError

    def ga4_window(self, start: dt.date, end: dt.date) -> str:
        raise NotImplementedError

    # -- expressions --------------------------------------------------------
    def event_date(self) -> str:
        raise NotImplementedError

    def param_str(self, key: str) -> str:
        raise NotImplementedError

    def param_num(self, key: str) -> str:
        raise NotImplementedError

    def param_int(self, key: str) -> str:
        raise NotImplementedError

    def date_lit(self, d: dt.date) -> str:
        return f"DATE '{d.isoformat()}'"

    def add_days(self, expr: str, n: int) -> str:
        raise NotImplementedError

    def days_between(self, later: str, earlier: str) -> str:
        raise NotImplementedError

    def micros_to_date(self, expr: str) -> str:
        raise NotImplementedError

    def ts_to_date(self, expr: str) -> str:
        raise NotImplementedError

    def ts_lit(self, d: dt.date) -> str:
        return f"TIMESTAMP '{d.isoformat()} 00:00:00'"

    def date_spine(self, start: dt.date, end: dt.date) -> str:
        """A SELECT producing one row per date in [start, end] in column `day`."""
        raise NotImplementedError

    int_type = "BIGINT"
    float_type = "DOUBLE"


class BigQueryDialect(Dialect):
    name = "bigquery"
    int_type = "INT64"
    float_type = "FLOAT64"

    def __init__(self, project: str, dataset: str):
        self.project = check_ident(project, "project")
        self.dataset = check_ident(dataset, "dataset")

    def ga4_events(self) -> str:
        return f"`{self.project}.{self.dataset}.events_*`"

    def ga4_window(self, start: dt.date, end: dt.date) -> str:
        # Daily tables only; events_intraday_* suffixes sort after digits.
        return f"_TABLE_SUFFIX BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'"

    def table(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{check_ident(name, 'table')}`"

    def event_date(self) -> str:
        return "PARSE_DATE('%Y%m%d', event_date)"

    def param_str(self, key: str) -> str:
        return f"(SELECT value.string_value FROM UNNEST(event_params) WHERE key = '{check_key(key)}')"

    def param_num(self, key: str) -> str:
        return (
            "(SELECT COALESCE(CAST(value.int_value AS FLOAT64), value.double_value, "
            "CAST(value.float_value AS FLOAT64), SAFE_CAST(value.string_value AS FLOAT64)) "
            f"FROM UNNEST(event_params) WHERE key = '{check_key(key)}')"
        )

    def param_int(self, key: str) -> str:
        return f"(SELECT value.int_value FROM UNNEST(event_params) WHERE key = '{check_key(key)}')"

    def add_days(self, expr: str, n: int) -> str:
        return f"DATE_ADD({expr}, INTERVAL {int(n)} DAY)"

    def days_between(self, later: str, earlier: str) -> str:
        return f"DATE_DIFF({later}, {earlier}, DAY)"

    def micros_to_date(self, expr: str) -> str:
        return f"DATE(TIMESTAMP_MICROS({expr}))"

    def ts_to_date(self, expr: str) -> str:
        return f"DATE({expr})"

    def date_spine(self, start: dt.date, end: dt.date) -> str:
        return (
            f"SELECT day FROM UNNEST(GENERATE_DATE_ARRAY({self.date_lit(start)}, "
            f"{self.date_lit(end)})) AS day"
        )


DUCKDB_MACROS = [
    """CREATE OR REPLACE MACRO ga4_param(params, k) AS
       list_filter(params, lambda p: p.key = k)[1].value""",
    """CREATE OR REPLACE MACRO ga4_param_str(params, k) AS ga4_param(params, k).string_value""",
    """CREATE OR REPLACE MACRO ga4_param_int(params, k) AS ga4_param(params, k).int_value""",
    """CREATE OR REPLACE MACRO ga4_param_num(params, k) AS COALESCE(
           CAST(ga4_param(params, k).int_value AS DOUBLE),
           ga4_param(params, k).double_value,
           CAST(ga4_param(params, k).float_value AS DOUBLE),
           TRY_CAST(ga4_param(params, k).string_value AS DOUBLE))""",
]


class DuckDBDialect(Dialect):
    """Runs the same extraction against GA4-export-shaped data in DuckDB.

    The executor exposes the data as views `ga4_events` and `crashlytics_events`
    and installs the ga4_param_* macros.
    """

    name = "duckdb"

    def ga4_events(self) -> str:
        return "ga4_events"

    def ga4_window(self, start: dt.date, end: dt.date) -> str:
        return f"event_date BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}'"

    def table(self, name: str) -> str:
        return check_ident(name, "table")

    def event_date(self) -> str:
        return "CAST(strptime(event_date, '%Y%m%d') AS DATE)"

    def param_str(self, key: str) -> str:
        return f"ga4_param_str(event_params, '{check_key(key)}')"

    def param_num(self, key: str) -> str:
        return f"ga4_param_num(event_params, '{check_key(key)}')"

    def param_int(self, key: str) -> str:
        return f"ga4_param_int(event_params, '{check_key(key)}')"

    def add_days(self, expr: str, n: int) -> str:
        return f"CAST(({expr}) + ({int(n)}) AS DATE)"

    def days_between(self, later: str, earlier: str) -> str:
        return f"CAST(date_diff('day', {earlier}, {later}) AS BIGINT)"

    def micros_to_date(self, expr: str) -> str:
        return f"CAST(make_timestamp({expr}) AS DATE)"

    def ts_to_date(self, expr: str) -> str:
        return f"CAST({expr} AS DATE)"

    def date_spine(self, start: dt.date, end: dt.date) -> str:
        return (
            "SELECT CAST(generate_series AS DATE) AS day FROM generate_series("
            f"TIMESTAMP '{start.isoformat()}', TIMESTAMP '{end.isoformat()}', INTERVAL 1 DAY)"
        )
