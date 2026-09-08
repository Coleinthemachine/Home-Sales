from datetime import date, datetime, timezone

import pytest

from home_sales.models import Sale
from home_sales.store import Store

RANKS = {"county": 90, "news": 10}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as store:
        yield store


def make_sale(**overrides) -> Sale:
    defaults = dict(
        address="123 N Wayne Ave",
        price=1_500_000,
        sale_date=date(2026, 3, 1),
        source="county",
        county="Delaware",
        municipality="Radnor Township",
        zip_code="19087",
        area="Radnor Township",
    )
    defaults.update(overrides)
    return Sale(**defaults)


def test_insert_then_read_back(store):
    assert store.upsert(make_sale(), RANKS) is True
    sales = store.query_sales()
    assert len(sales) == 1
    assert sales[0].price == 1_500_000
    assert sales[0].municipality == "Radnor Township"


def test_same_sale_from_two_sources_merges(store):
    assert store.upsert(make_sale(source="county"), RANKS) is True
    assert store.upsert(make_sale(source="news", price=1_499_000, sqft=4200), RANKS) is False

    sales = store.query_sales()
    assert len(sales) == 1
    assert sales[0].price == 1_500_000  # authoritative source wins
    assert sales[0].sqft == 4200        # but the extra detail is kept


def test_differently_formatted_address_still_dedupes(store):
    store.upsert(make_sale(address="123 North Wayne Avenue"), RANKS)
    store.upsert(make_sale(address="123 N. Wayne Ave.", source="news"), RANKS)
    assert len(store.query_sales()) == 1


def test_dates_within_window_are_one_transaction(store):
    """Deed date and settlement date differ; that's one sale, not two."""
    store.upsert(make_sale(sale_date=date(2026, 3, 1)), RANKS)
    store.upsert(make_sale(sale_date=date(2026, 3, 9), source="news"), RANKS)
    assert len(store.query_sales()) == 1


def test_dates_outside_window_are_separate_sales(store):
    """The same house genuinely resold months later."""
    store.upsert(make_sale(sale_date=date(2026, 3, 1)), RANKS)
    store.upsert(make_sale(sale_date=date(2026, 9, 1)), RANKS)
    assert len(store.query_sales()) == 2


def test_different_units_are_separate_sales(store):
    store.upsert(make_sale(address="500 Lancaster Ave Apt 1"), RANKS)
    store.upsert(make_sale(address="500 Lancaster Ave Apt 2"), RANKS)
    assert len(store.query_sales()) == 2


def test_query_filters(store):
    store.upsert(make_sale(price=1_200_000, sale_date=date(2026, 3, 1)), RANKS)
    store.upsert(
        make_sale(
            address="9 Old Gulph Rd", price=3_000_000, sale_date=date(2026, 2, 1),
            municipality="Lower Merion", county="Montgomery", zip_code="19035",
            area="Montgomery County",
        ),
        RANKS,
    )

    assert len(store.query_sales(min_price=2_000_000)) == 1
    assert len(store.query_sales(since=date(2026, 2, 15))) == 1
    assert len(store.query_sales(area="Montgomery County")) == 1
    assert len(store.query_sales(search="Gulph")) == 1
    assert len(store.query_sales(search="Radnor")) == 1
    assert store.count_sales() == 2


def test_results_are_newest_first(store):
    store.upsert(make_sale(address="1 A St", sale_date=date(2026, 1, 1)), RANKS)
    store.upsert(make_sale(address="2 B St", sale_date=date(2026, 3, 1)), RANKS)
    assert [s.sale_date for s in store.query_sales()] == [date(2026, 3, 1), date(2026, 1, 1)]


def test_distinct_values_rejects_unexpected_column(store):
    with pytest.raises(ValueError):
        store.distinct_values("price; DROP TABLE sales")


def test_source_run_status_records_failures(store):
    started = datetime.now(timezone.utc)
    store.record_source_run("run1", "county", "ok", started, 10, 4)
    store.record_source_run("run1", "zillow", "blocked", started, 0, 0, "robots.txt disallows")
    store.record_source_run("run2", "county", "error", started, 0, 0, "timeout")

    statuses = {row["source"]: row for row in store.latest_source_status()}
    assert statuses["county"]["status"] == "error"   # most recent run wins
    assert statuses["zillow"]["message"] == "robots.txt disallows"


def test_raw_payload_survives_roundtrip(store):
    store.upsert(make_sale(raw={"PARCEL": "36-01-0042", "nested": {"a": 1}}), RANKS)
    assert store.query_sales()[0].raw["nested"] == {"a": 1}
