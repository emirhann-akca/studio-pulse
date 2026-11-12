"""AST guard for SQL that this package did not write (a model's, a user's).

Layer 1 of 3 (layer 2 is the locked DuckDB session in executor.py, layer 3
its row and time limits). The SQL is parsed with sqlglot in the DuckDB
dialect and refused unless it is

  * exactly one statement, a SELECT (or a UNION/INTERSECT/EXCEPT of SELECTs);
  * free of write, DDL, COPY/EXPORT, ATTACH, PRAGMA, SET, INSTALL/LOAD, CALL;
  * reading only allow-listed tables (or its own CTEs), never a table
    function (read_csv, glob, range, query_table, ...), a file path or
    another catalog/schema than main;
  * using only typed sqlglot functions or allow-listed DuckDB names
    (no getenv, json_execute_serialized_sql, duckdb_settings, ...);
  * referring only to columns that exist (sqlglot qualify with
    validate_qualify_columns, the research's catch of an invented fail_rate).

The returned SQL is sqlglot's re-rendering of the checked tree with a LIMIT
of at most max_rows: what runs is exactly what was checked. LLM-written SQL
is executed on the local warehouse only; it is never transpiled to BigQuery
(sqlglot turned date_trunc on a DATE into TIMESTAMP_TRUNC and quantile_cont
into PERCENTILE_CONT without OVER in the research's run).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

# sqlglot logs "falling back to parsing as a Command" for LOAD / CALL; those are refused anyway.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_ROWS = 500

# DuckDB functions sqlglot does not type (they parse as exp.Anonymous) that are
# pure computations on values. Anything else untyped is refused.
SAFE_FUNCTIONS = frozenset(
    {
        "arg_min", "arg_max", "argmin", "argmax", "mode", "kurtosis", "skewness", "entropy",
        "product", "fsum", "favg", "bit_count", "isfinite", "isinf", "isnan", "make_date",
        "last_day", "dayname", "monthname", "yearweek", "weekofyear", "dayofweek", "dayofyear",
        "isodow", "epoch", "epoch_ms", "date_part", "datepart", "datesub", "date_sub",
        "quantile", "quantile_disc", "quantile_cont", "median", "percent_rank", "cume_dist",
        "ntile", "nth_value", "list_aggregate", "list_sum", "list_avg", "list_min", "list_max",
        "list_count", "list_sort", "list_distinct", "array_length", "len", "length",
        "string_agg", "group_concat", "listagg", "regexp_extract", "regexp_replace",
        "regexp_matches", "starts_with", "ends_with", "contains", "lpad", "rpad", "repeat",
        "strip_accents", "lcase", "ucase", "trim", "ltrim", "rtrim", "left", "right",
        "round_even", "roundbankers", "trunc", "sign", "even", "factorial", "gamma",
        "lgamma", "ln", "log10", "log2", "radians", "degrees", "pi", "nextafter",
        "greatest", "least", "coalesce", "ifnull", "nullif", "if", "iif", "typeof",
        "try_cast", "bool_and", "bool_or", "count_if", "countif", "first", "last", "any_value",
        "approx_count_distinct", "approx_quantile", "reservoir_quantile", "histogram",
        "regr_slope", "regr_intercept", "regr_r2", "corr", "covar_pop", "covar_samp",
        "stddev", "stddev_samp", "stddev_pop", "var_samp", "var_pop", "variance",
        "date_diff", "datediff", "date_trunc", "datetrunc", "date_add", "time_bucket",
        "strftime", "strptime", "to_days", "to_weeks", "to_months", "to_years",
    }
)  # fmt: skip

# Typed sqlglot nodes that must never appear, wherever they are in the tree.
_FORBIDDEN_NODES = tuple(
    getattr(exp, n)
    for n in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Command", "Pragma", "Set", "Attach", "Detach", "Copy", "Install", "Into", "Use",
        "Describe", "Summarize", "Show", "Transaction", "Commit", "Rollback", "LoadData",
        "Export", "ReadCSV", "ReadParquet", "Kill", "Grant", "Revoke",
    )
    if hasattr(exp, n)
)  # fmt: skip
_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class Refused(ValueError):
    """The SQL was refused; `reason` is shown to the user and stored with the answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Checked:
    sql: str  # what will run (re-rendered, LIMIT injected)
    tables: tuple[str, ...]
    limited: bool  # a LIMIT was added or lowered


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:200]


def check(sql: str, schema: dict[str, dict[str, str]], *, max_rows: int = MAX_ROWS) -> Checked:
    """Validate `sql` against the allow-listed `schema` {table: {column: type}}."""
    if not sql or not sql.strip():
        raise Refused("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as e:
        raise Refused(f"could not parse the query: {_first_line(e)}") from None
    except SqlglotError as e:
        raise Refused(f"could not parse the query: {_first_line(e)}") from None
    if len(statements) != 1:
        raise Refused("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, _ROOTS):
        raise Refused(f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise Refused(f"forbidden statement or clause: {type(node).__name__.upper()}")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    allowed = {t.lower() for t in schema}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise Refused(f"table functions are not allowed: {t.sql('duckdb')[:80]}")
        name = t.name.lower()
        if t.catalog or (t.db and t.db.lower() != "main"):
            raise Refused(f"only the warehouse's main schema is allowed: {t.sql('duckdb')[:80]}")
        if name in ctes and not t.db:
            continue
        if name not in allowed:
            raise Refused(f"table not allowed: {t.name}")
        tables.append(name)
    for f in tree.find_all(exp.Anonymous):
        fname = (f.name or "").lower()
        if fname not in SAFE_FUNCTIONS:
            raise Refused(f"function not allowed: {f.name}")
    try:
        tree = qualify(
            tree,
            schema=schema,
            dialect="duckdb",
            validate_qualify_columns=True,
            quote_identifiers=False,
        )
    except OptimizeError as e:
        raise Refused(f"unknown column or table: {_first_line(e)}") from None
    except SqlglotError as e:
        raise Refused(f"query not understood: {_first_line(e)}") from None
    except Exception as e:  # fail closed on anything the optimizer did not expect
        raise Refused(f"query not understood: {type(e).__name__}") from None
    tree, limited = _limit(tree, max_rows)
    return Checked(tree.sql(dialect="duckdb"), tuple(sorted(set(tables))), limited)


def _limit(tree: exp.Expression, max_rows: int) -> tuple[exp.Expression, bool]:
    if isinstance(tree, exp.Select):
        lim = tree.args.get("limit")
        current = None
        if lim is not None:
            node = lim.expression if isinstance(lim, exp.Limit) else lim
            try:
                current = int(node.name) if isinstance(node, exp.Literal) else None
            except ValueError:
                current = None
        if current is not None and 0 <= current <= max_rows:
            return tree, False
        return tree.limit(max_rows, copy=True), True
    wrapped = exp.select("*").from_(tree.subquery("_q")).limit(max_rows)
    return wrapped, True
