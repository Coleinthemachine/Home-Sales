"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from . import discovery, pipeline
from .config import load_config
from .http import Fetcher
from .sources import registered_types
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.days:
        config.lookback_days = args.days
    if args.min_price:
        config.min_price = args.min_price

    with Store(config.database) as store:
        result = pipeline.run(config, store, only=args.source, dry_run=args.dry_run)

    print(result.summary())
    if args.dry_run:
        print("\n(dry run -- nothing written)")
        for sale in result.kept_sales[:20]:
            print(f"  {sale.sale_date}  ${sale.price:>10,}  {sale.display_address}  [{sale.area}]")

    failures = [r for r in result.results if r.status in ("error", "misconfigured")]
    if failures:
        print(
            f"\n{len(failures)} source(s) need attention. "
            "For ArcGIS layers, run: python -m home_sales discover <service-url>",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(args.config)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config) if Path(args.config or "config.toml").exists() else None
    fetcher = Fetcher(
        user_agent=config.user_agent if config else "home-sales-bot/0.1 (discovery)",
        delay_seconds=config.request_delay_seconds if config else 1.0,
        respect_robots=config.respect_robots if config else True,
    )

    url = args.url.rstrip("/")
    if discovery.looks_like_layer(url):
        report = discovery.inspect_layer(fetcher, url)
        print(f"Layer: {report.name}\nURL:   {report.url}\n")
        print(f"{len(report.fields)} fields:")
        for field in report.fields:
            print(f"  {field['name']:<32} {field['type']}")
        if report.sample:
            print("\nSample record:")
            print(json.dumps(report.sample, indent=2, default=str)[:1500])
        print("\n--- Suggested config.toml block (verify before use) ---\n")
        print(discovery.render_toml(args.name or "county_source", report, args.county))
        return 0

    children = discovery.list_children(fetcher, url)
    if children["folders"]:
        print("Folders:")
        for folder in children["folders"]:
            print(f"  {url}/{folder}")
    if children["services"]:
        print("\nServices:")
        for service in children["services"]:
            print(f"  {url}/{service['name'].split('/')[-1]}/{service['type']}")
    for key in ("layers", "tables"):
        if children[key]:
            print(f"\n{key.title()}:")
            for layer in children[key]:
                print(f"  [{layer.get('id')}] {layer.get('name')}  ->  {url}/{layer.get('id')}")
    if not any(children.values()):
        print("Nothing found at that URL. Is it an ArcGIS REST endpoint?")
        return 1
    print("\nRe-run discover against a specific layer URL to get field suggestions.")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"Registered source types: {', '.join(registered_types())}\n")
    width = max((len(s.name) for s in config.sources), default=8)
    for spec in config.sources:
        state = "enabled" if spec.enabled else "disabled"
        print(f"  {spec.name.ljust(width)}  {spec.type:<12} rank={spec.rank:<4} {state}")

    with Store(config.database) as store:
        statuses = store.latest_source_status()
    if statuses:
        print("\nLast run status:")
        for status in statuses:
            message = f" -- {status['message']}" if status["message"] else ""
            print(
                f"  {status['source'].ljust(width)}  {status['status'].upper():<13}"
                f" fetched={status['fetched']:<5} kept={status['kept']}{message}"
            )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    since = date.today() - timedelta(days=args.days) if args.days else None
    with Store(config.database) as store:
        sales = store.query_sales(min_price=args.min_price or config.min_price, since=since, limit=1_000_000)

    columns = [
        "sale_date", "price", "address", "unit", "municipality", "county", "zip_code",
        "area", "beds", "baths", "sqft", "lot_acres", "year_built", "buyer", "seller",
        "parcel_id", "source", "source_url",
    ]
    handle = open(args.output, "w", newline="") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for sale in sales:
            writer.writerow(sale.to_row())
    finally:
        if args.output:
            handle.close()
            print(f"Wrote {len(sales)} sales to {args.output}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="home_sales",
        description="Track $1M+ home sales in Chester, Montgomery and Delaware counties, PA.",
    )
    parser.add_argument("-c", "--config", default="config.toml", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="collect sales from all enabled sources")
    run_parser.add_argument("--source", action="append", help="only run this source (repeatable)")
    run_parser.add_argument("--days", type=int, help="override lookback window")
    run_parser.add_argument("--min-price", type=int, dest="min_price", help="override price floor")
    run_parser.add_argument("--dry-run", action="store_true", help="collect without writing")
    run_parser.set_defaults(func=cmd_run)

    serve_parser = subparsers.add_parser("serve", help="run the dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)
    serve_parser.add_argument("--debug", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    discover_parser = subparsers.add_parser(
        "discover", help="inspect an ArcGIS endpoint and suggest config"
    )
    discover_parser.add_argument("url", help="ArcGIS server, service, or layer URL")
    discover_parser.add_argument("--name", help="source name for the generated config block")
    discover_parser.add_argument("--county", help="county name to attach to records")
    discover_parser.set_defaults(func=cmd_discover)

    sources_parser = subparsers.add_parser("sources", help="list configured sources and health")
    sources_parser.set_defaults(func=cmd_sources)

    export_parser = subparsers.add_parser("export", help="export collected sales as CSV")
    export_parser.add_argument("-o", "--output", help="output file (default: stdout)")
    export_parser.add_argument("--days", type=int, help="only sales in the last N days")
    export_parser.add_argument("--min-price", type=int, dest="min_price")
    export_parser.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
