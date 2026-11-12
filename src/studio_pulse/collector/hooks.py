"""The two places the core pipeline calls into the collector package.

- config.load_config -> add_registry_apps: registry apps join cfg.apps
- pipeline._ingest_source("pulse_sdk") -> ingest_pulse_sdk
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import ConfigError


def add_registry_apps(cfg: Any) -> None:
    from .registry import registry_apps

    cfg.apps.extend(registry_apps(cfg))


def ingest_pulse_sdk(
    con: Any,
    cfg: Any,
    app: Any,
    name: str,
    src: dict[str, Any],
    start: dt.date,
    end: dt.date,
    log: Any,
    *,
    dry_run: bool = False,
    bq_client: Any = None,
    bq_stats: list[Any] | None = None,
) -> Any:
    from .ingest import ingest
    from .settings import from_config

    bundles = [str(b) for b in (src.get("bundle_ids") or [])]
    if not bundles:
        raise ConfigError(f"app {app.id!r}: pulse_sdk needs bundle_ids")
    bq = src.get("bigquery") or (cfg.raw or {}).get("collector", {}).get("bigquery")
    if bq:  # cloud mode: the collector writes BigQuery (deploy/cloudrun); read the window there
        from ..connectors.dialect import BigQueryDialect
        from ..connectors.executors import BigQueryExecutor
        from .ingest import bigquery_registry_sql, bigquery_sql, stage_rows

        dialect = BigQueryDialect(bq["project"], bq.get("dataset", "pulse_raw"))
        ex = BigQueryExecutor(
            billing_project=bq.get("billing_project", bq["project"]),
            max_bytes_billed=min(
                int(bq.get("max_bytes_billed", cfg.max_bytes_billed)), cfg.max_bytes_billed
            ),
            location=bq.get("location", "US"),
            dry_run_only=dry_run,
            client=bq_client,
            log=log,
        )

        if dry_run:  # estimate both queries against the cap; never stage or write anything
            try:
                for t in ("events", "quarantine"):
                    ex.run(f"pulse_sdk.{t}", bigquery_sql(dialect, t, start, end))
            finally:
                if bq_stats is not None:
                    bq_stats.extend(ex.stats)
            return "skipped (dry run)"

        def stage(c: Any, tag: str) -> int:
            got = {t: ex.run(f"pulse_sdk.{t}", bigquery_sql(dialect, t, start, end))
                   for t in ("events", "quarantine")}  # fmt: skip
            return stage_rows(c, tag, got)

        try:
            reg = ex.run("pulse_sdk.app_registry", bigquery_registry_sql(dialect))
            return ingest(
                con,
                app_id=app.id,
                source=name,
                data_dir=None,
                registry=reg.to_pylist() if reg is not None else [],
                bundle_ids=bundles,
                timezone=app.timezone,
                start=start,
                end=end,
                log=log,
                stage=stage,
                stage_key=f"bq:{dialect.project}.{dialect.dataset}",
            )
        finally:
            if bq_stats is not None:
                bq_stats.extend(ex.stats)
    data_dir = cfg.resolve(src["data_dir"]) if src.get("data_dir") else from_config(cfg).data_dir
    return ingest(
        con,
        app_id=app.id,
        source=name,
        data_dir=data_dir,
        bundle_ids=bundles,
        timezone=app.timezone,
        start=start,
        end=end,
        log=log,
    )
