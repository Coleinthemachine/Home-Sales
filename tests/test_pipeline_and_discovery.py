from datetime import date

import pytest

from home_sales import discovery, pipeline
from home_sales.config import SourceConfig
from home_sales.http import RobotsDisallowed
from home_sales.models import Sale
from home_sales.sources import ConfigurationError, Source, register
from home_sales.store import Store


@register("_test_static")
class StaticSource(Source):
    """Yields whatever the test put in options, so pipeline logic is testable offline."""

    def fetch(self):
        for item in self.options.get("sales", []):
            yield Sale(**item)


@register("_test_failing")
class FailingSource(Source):
    def fetch(self):
        raise {"robots": RobotsDisallowed, "config": ConfigurationError}.get(
            self.options.get("mode"), RuntimeError
        )("boom")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "run.db") as store:
        yield store


def sale_payload(**overrides):
    payload = dict(
        address="123 N Wayne Ave",
        price=1_500_000,
        sale_date=date(2026, 3, 1),
        source="county",
        county="Delaware",
        municipality="Radnor Township",
        zip_code="19087",
    )
    payload.update(overrides)
    return payload


def test_run_stores_qualifying_sales(config, store):
    config.sources = [
        SourceConfig(
            name="county",
            type="_test_static",
            rank=90,
            options={"sales": [sale_payload(), sale_payload(price=900_000, address="2 Low St")]},
        )
    ]
    result = pipeline.run(config, store, today=date(2026, 3, 20))

    assert result.new_sales == 1
    assert result.results[0].status == "ok"
    assert result.results[0].fetched == 2
    assert result.results[0].kept == 1
    assert "below_price=1" in result.results[0].message
    assert store.count_sales() == 1


def test_run_merges_across_sources_in_one_pass(config, store):
    config.sources = [
        SourceConfig(name="county", type="_test_static", rank=90,
                     options={"sales": [sale_payload(source="county")]}),
        SourceConfig(name="news", type="_test_static", rank=10,
                     options={"sales": [sale_payload(source="news", price=1_490_000,
                                                     address="123 North Wayne Avenue")]}),
    ]
    result = pipeline.run(config, store, today=date(2026, 3, 20))

    assert result.new_sales == 1
    assert result.updated_sales == 1
    assert store.count_sales() == 1
    assert store.query_sales()[0].price == 1_500_000


def test_run_isolates_source_failures(config, store):
    config.sources = [
        SourceConfig(name="good", type="_test_static", rank=90,
                     options={"sales": [sale_payload()]}),
        SourceConfig(name="blocked", type="_test_failing", options={"mode": "robots"}),
        SourceConfig(name="broken", type="_test_failing", options={"mode": "config"}),
        SourceConfig(name="crashed", type="_test_failing", options={"mode": "other"}),
    ]
    result = pipeline.run(config, store, today=date(2026, 3, 20))

    statuses = {r.name: r.status for r in result.results}
    assert statuses == {
        "good": "ok",
        "blocked": "blocked",
        "broken": "misconfigured",
        "crashed": "error",
    }
    assert result.new_sales == 1          # the healthy source still landed
    assert result.ok is False

    recorded = {row["source"]: row["status"] for row in store.latest_source_status()}
    assert recorded["blocked"] == "blocked"


def test_dry_run_writes_nothing(config, store):
    config.sources = [
        SourceConfig(name="county", type="_test_static", rank=90,
                     options={"sales": [sale_payload()]})
    ]
    result = pipeline.run(config, store, dry_run=True, today=date(2026, 3, 20))

    assert len(result.kept_sales) == 1
    assert store.count_sales() == 0


def test_run_honors_source_filter(config, store):
    config.sources = [
        SourceConfig(name="a", type="_test_static", options={"sales": [sale_payload()]}),
        SourceConfig(name="b", type="_test_static",
                     options={"sales": [sale_payload(address="9 Old Gulph Rd")]}),
    ]
    result = pipeline.run(config, store, only=["a"], today=date(2026, 3, 20))
    assert [r.name for r in result.results] == ["a"]


def test_disabled_sources_are_skipped(config, store):
    config.sources = [
        SourceConfig(name="off", type="_test_static", enabled=False,
                     options={"sales": [sale_payload()]})
    ]
    result = pipeline.run(config, store, today=date(2026, 3, 20))
    assert result.results == []
    assert store.count_sales() == 0


# --- Discovery ------------------------------------------------------------

FIELDS = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID"},
    {"name": "PROP_ADDRESS", "type": "esriFieldTypeString"},
    {"name": "MUNICIPALITY", "type": "esriFieldTypeString"},
    {"name": "PROP_ZIP", "type": "esriFieldTypeString"},
    {"name": "SALE_PRICE", "type": "esriFieldTypeDouble"},
    {"name": "LAST_PRICE", "type": "esriFieldTypeDouble"},
    {"name": "SALE_DATE", "type": "esriFieldTypeDate"},
    {"name": "GRANTEE", "type": "esriFieldTypeString"},
    {"name": "GRANTOR", "type": "esriFieldTypeString"},
    {"name": "PARCEL_ID", "type": "esriFieldTypeString"},
    {"name": "YEAR_BUILT", "type": "esriFieldTypeInteger"},
]


def test_suggest_fields_prefers_exact_matches():
    suggestions = discovery.suggest_fields(FIELDS)
    assert suggestions["price"] == "SALE_PRICE"     # not LAST_PRICE
    assert suggestions["sale_date"] == "SALE_DATE"
    assert suggestions["address"] == "PROP_ADDRESS"
    assert suggestions["municipality"] == "MUNICIPALITY"
    assert suggestions["buyer"] == "GRANTEE"
    assert suggestions["seller"] == "GRANTOR"
    assert suggestions["year_built"] == "YEAR_BUILT"


def test_suggest_fields_respects_types():
    """A text column named 'price' isn't a usable price field."""
    suggestions = discovery.suggest_fields(
        [{"name": "PRICE", "type": "esriFieldTypeString"},
         {"name": "AMOUNT", "type": "esriFieldTypeDouble"}]
    )
    assert suggestions.get("price") == "AMOUNT"


def test_looks_like_layer():
    assert discovery.looks_like_layer("https://x/arcgis/rest/services/P/FeatureServer/0")
    assert discovery.looks_like_layer("https://x/arcgis/rest/services/P/MapServer/12/")
    assert not discovery.looks_like_layer("https://x/arcgis/rest/services/P/FeatureServer")
    assert not discovery.looks_like_layer("https://x/arcgis/rest/services")


def test_render_toml_produces_usable_block():
    report = discovery.LayerReport(
        url="https://x/arcgis/rest/services/Parcels/FeatureServer/0",
        name="Parcels",
        fields=FIELDS,
        suggestions=discovery.suggest_fields(FIELDS),
        date_literal="date",
    )
    block = discovery.render_toml("delaware_county", report, county="Delaware")

    assert "[sources.delaware_county]" in block
    assert 'price_field = "SALE_PRICE"' in block
    assert 'county = "Delaware"' in block
    assert 'address = "PROP_ADDRESS"' in block
    assert "SET_ME" not in block


def test_render_toml_flags_missing_required_fields():
    report = discovery.LayerReport(
        url="https://x/FeatureServer/0", name="Layer", fields=[], suggestions={}, date_literal="date"
    )
    block = discovery.render_toml("mystery", report)
    assert "SET_ME_price_field" in block
    assert "SET_ME_address_field" in block


def test_date_literal_matches_storage_type():
    numeric = [{"name": "SALE_DATE", "type": "esriFieldTypeDouble"}]
    text = [{"name": "SALE_DATE", "type": "esriFieldTypeString"}]
    assert discovery._date_literal_for(numeric, "SALE_DATE") == "epoch_ms"
    assert discovery._date_literal_for(text, "SALE_DATE") == "string"
    assert discovery._date_literal_for(FIELDS, "SALE_DATE") == "date"
