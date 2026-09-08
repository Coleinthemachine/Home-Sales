"""Collect -> filter -> dedupe -> store."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from .config import Config
from .http import Fetcher, RobotsDisallowed
from .models import Sale
from .sources import ConfigurationError, build_source
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    name: str
    status: str
    fetched: int = 0
    kept: int = 0
    message: str | None = None


@dataclass
class RunResult:
    run_id: str
    started_at: datetime
    results: list[SourceResult] = field(default_factory=list)
    new_sales: int = 0
    updated_sales: int = 0
    kept_sales: list[Sale] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.status in ("ok", "skipped") for result in self.results)

    def summary(self) -> str:
        lines = [
            f"Run {self.run_id}: {self.new_sales} new, {self.updated_sales} updated",
            "",
        ]
        width = max((len(r.name) for r in self.results), default=6)
        for result in self.results:
            detail = f" -- {result.message}" if result.message else ""
            lines.append(
                f"  {result.name.ljust(width)}  {result.status.upper():<9} "
                f"fetched={result.fetched:<5} kept={result.kept}{detail}"
            )
        return "\n".join(lines)


def _is_qualifying(sale: Sale, config: Config, earliest: date, today: date) -> str | None:
    """Return a rejection reason, or None if the sale qualifies."""
    if sale.price < config.min_price:
        return "below_price"
    if sale.sale_date < earliest:
        return "too_old"
    if sale.sale_date > today + timedelta(days=1):
        return "future_date"
    area = config.match_area(sale.normalized_county, sale.normalized_municipality, sale.zip_code)
    if area is None:
        return "outside_area"
    sale.area = area.name
    return None


def filter_sales(
    sales: Iterable[Sale], config: Config, *, today: date | None = None
) -> tuple[list[Sale], dict[str, int]]:
    """Keep qualifying sales and count why the rest were dropped."""
    today = today or date.today()
    earliest = today - timedelta(days=config.lookback_days)
    kept: list[Sale] = []
    rejected: dict[str, int] = {}
    for sale in sales:
        reason = _is_qualifying(sale, config, earliest, today)
        if reason is None:
            kept.append(sale)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    return kept, rejected


def run(
    config: Config,
    store: Store,
    *,
    only: Sequence[str] | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> RunResult:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    result = RunResult(run_id=run_id, started_at=datetime.now(timezone.utc))
    if not dry_run:
        store.start_run(run_id)

    fetcher = Fetcher(
        user_agent=config.user_agent,
        delay_seconds=config.request_delay_seconds,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        respect_robots=config.respect_robots,
    )
    ranks = config.source_ranks

    for spec in config.enabled_sources():
        if only and spec.name not in only:
            continue

        started = datetime.now(timezone.utc)
        source_result = SourceResult(name=spec.name, status="ok")
        try:
            source = build_source(spec, config, fetcher)
            fetched = list(source.fetch())
            source_result.fetched = len(fetched)

            kept, rejected = filter_sales(fetched, config, today=today)
            source_result.kept = len(kept)
            if rejected:
                source_result.message = "dropped: " + ", ".join(
                    f"{reason}={count}" for reason, count in sorted(rejected.items())
                )

            for sale in kept:
                result.kept_sales.append(sale)
                if not dry_run:
                    if store.upsert(sale, ranks, config.dedupe_window_days):
                        result.new_sales += 1
                    else:
                        result.updated_sales += 1

        except RobotsDisallowed as exc:
            source_result.status = "blocked"
            source_result.message = str(exc)
            log.warning("%s blocked by robots.txt: %s", spec.name, exc)
        except ConfigurationError as exc:
            source_result.status = "misconfigured"
            source_result.message = str(exc)
            log.error("%s misconfigured: %s", spec.name, exc)
        except Exception as exc:
            source_result.status = "error"
            source_result.message = f"{type(exc).__name__}: {exc}"
            log.exception("%s failed", spec.name)

        result.results.append(source_result)
        if not dry_run:
            store.record_source_run(
                run_id,
                spec.name,
                source_result.status,
                started,
                source_result.fetched,
                source_result.kept,
                source_result.message,
            )

    if not dry_run:
        store.finish_run(run_id, result.new_sales, result.updated_sales)
    return result
