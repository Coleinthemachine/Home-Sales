"""Dashboard application.

Read-only over the SQLite store. Opening a fresh connection per request keeps
this safe alongside a concurrent `run` writing to the same database.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, render_template, request

from ..config import Config, load_config
from ..store import Store

DEFAULT_WINDOW_DAYS = 90


def _int_arg(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _query_filters(config: Config) -> dict[str, Any]:
    days = _int_arg("days", DEFAULT_WINDOW_DAYS, minimum=1, maximum=3650)
    min_price = _int_arg("min_price", config.min_price, minimum=0)
    return {
        "days": days,
        "min_price": min_price,
        "since": date.today() - timedelta(days=days),
        "area": (request.args.get("area") or "").strip() or None,
        "search": (request.args.get("q") or "").strip() or None,
    }


def create_app(config_path: str | Path | None = None) -> Flask:
    config = load_config(config_path)
    app = Flask(__name__)
    app.config["HOME_SALES_CONFIG"] = config

    def open_store() -> Store:
        return Store(config.database)

    @app.template_filter("money")
    def money(value: Any) -> str:
        try:
            return f"${int(value):,}"
        except (TypeError, ValueError):
            return "--"

    @app.template_filter("prettydate")
    def prettydate(value: Any) -> str:
        if isinstance(value, date):
            return f"{value:%b} {value.day}, {value.year}"
        return str(value or "")

    @app.route("/")
    def index() -> str:
        filters = _query_filters(config)
        with open_store() as store:
            sales = store.query_sales(
                min_price=filters["min_price"],
                since=filters["since"],
                area=filters["area"],
                search=filters["search"],
                limit=500,
            )
            areas = store.distinct_values("area")
            source_status = store.latest_source_status()
            last_run = store.last_run()
            total = store.count_sales(min_price=config.min_price)

        volume = sum(sale.price for sale in sales)
        return render_template(
            "index.html",
            sales=sales,
            areas=areas,
            filters=filters,
            config=config,
            source_status=source_status,
            last_run=last_run,
            total_tracked=total,
            volume=volume,
            median=_median([sale.price for sale in sales]),
        )

    @app.route("/export.csv")
    def export_csv() -> Response:
        filters = _query_filters(config)
        with open_store() as store:
            sales = store.query_sales(
                min_price=filters["min_price"],
                since=filters["since"],
                area=filters["area"],
                search=filters["search"],
                limit=100_000,
            )

        buffer = io.StringIO()
        columns = [
            "sale_date", "price", "address", "unit", "municipality", "county",
            "zip_code", "area", "beds", "baths", "sqft", "buyer", "seller",
            "parcel_id", "source", "source_url",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for sale in sales:
            writer.writerow(sale.to_row())

        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=home-sales-{date.today().isoformat()}.csv"
            },
        )

    @app.route("/healthz")
    def healthz() -> dict[str, Any]:
        with open_store() as store:
            return {
                "ok": True,
                "tracked_sales": store.count_sales(),
                "last_run": store.last_run(),
                "sources": store.latest_source_status(),
            }

    return app


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) // 2
