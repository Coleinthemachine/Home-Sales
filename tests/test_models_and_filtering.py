from datetime import date, timedelta

from home_sales.models import Sale, build_sale
from home_sales.pipeline import filter_sales

RANKS = {"county": 90, "news": 10}


def make_sale(**overrides) -> Sale:
    defaults = dict(
        address="123 N Wayne Ave",
        price=1_500_000,
        sale_date=date(2026, 3, 1),
        source="county",
        county="Delaware",
        municipality="Radnor Township",
        zip_code="19087",
    )
    defaults.update(overrides)
    return Sale(**defaults)


def test_build_sale_rejects_unusable_records():
    assert build_sale(address="", price=1_500_000, sale_date="2026-03-01", source="s") is None
    assert build_sale(address="1 Elm St", price=None, sale_date="2026-03-01", source="s") is None
    assert build_sale(address="1 Elm St", price=1_500_000, sale_date=None, source="s") is None
    # A "street" with no number is a town name, not an address.
    assert build_sale(address="Wayne PA", price=1_500_000, sale_date="2026-03-01", source="s") is None


def test_build_sale_parses_messy_input():
    sale = build_sale(
        address="  123 north wayne avenue apt 2  ",
        price="$1,850,000",
        sale_date="03/01/2026",
        source="county",
        zip_code="19087-1234",
    )
    assert sale is not None
    assert sale.price == 1_850_000
    assert sale.sale_date == date(2026, 3, 1)
    assert sale.unit == "2"
    assert sale.zip_code == "19087"
    assert sale.display_address == "123 North Wayne Avenue #2"


def test_build_sale_ignores_unknown_fields():
    sale = build_sale(
        address="1 Elm St",
        price=1_100_000,
        sale_date="2026-01-05",
        source="s",
        nonsense_field="ignored",
    )
    assert sale is not None
    assert not hasattr(sale, "nonsense_field")


def test_merge_prefers_higher_ranked_source_and_fills_gaps():
    county = make_sale(source="county", price=1_500_000, buyer="Smith", beds=None)
    news = make_sale(source="news", price=1_499_000, buyer="Somebody Else", beds=5, sqft=4200)

    merged = county.merge(news, RANKS)

    assert merged.price == 1_500_000       # county wins the conflict
    assert merged.buyer == "Smith"
    assert merged.beds == 5                # news fills what county lacks
    assert merged.sqft == 4200
    assert merged.source == "county"


def test_merge_is_order_independent():
    county = make_sale(source="county", price=1_500_000)
    news = make_sale(source="news", price=1_499_000, sqft=4200)
    assert county.merge(news, RANKS).price == news.merge(county, RANKS).price == 1_500_000


def test_filter_keeps_qualifying_sale_and_labels_area(config):
    today = date(2026, 3, 20)
    kept, rejected = filter_sales([make_sale()], config, today=today)
    assert len(kept) == 1
    assert kept[0].area == "Radnor Township"
    assert rejected == {}


def test_filter_drops_below_threshold(config):
    kept, rejected = filter_sales(
        [make_sale(price=999_999)], config, today=date(2026, 3, 20)
    )
    assert kept == []
    assert rejected == {"below_price": 1}


def test_filter_drops_out_of_area(config):
    sale = make_sale(county="Bucks", municipality="Doylestown", zip_code="18901")
    kept, rejected = filter_sales([sale], config, today=date(2026, 3, 20))
    assert kept == []
    assert rejected == {"outside_area": 1}


def test_filter_drops_stale_and_future_dates(config):
    today = date(2026, 3, 20)
    stale = make_sale(sale_date=today - timedelta(days=config.lookback_days + 5))
    future = make_sale(sale_date=today + timedelta(days=10))
    kept, rejected = filter_sales([stale, future], config, today=today)
    assert kept == []
    assert rejected == {"too_old": 1, "future_date": 1}


def test_radnor_beats_delaware_county_label(config):
    """A sub-county area is more specific and should win the label."""
    sale = make_sale(municipality="Wayne", zip_code="19087", county="Delaware")
    kept, _ = filter_sales([sale], config, today=date(2026, 3, 20))
    assert kept[0].area == "Radnor Township"


def test_non_radnor_delaware_sale_labels_county(config):
    sale = make_sale(municipality="Media", zip_code="19063", county="Delaware")
    kept, _ = filter_sales([sale], config, today=date(2026, 3, 20))
    assert kept[0].area == "Delaware County"


def test_chester_and_montgomery_match_by_county(config):
    today = date(2026, 3, 20)
    chester = make_sale(county="Chester", municipality="West Chester", zip_code="19380")
    montco = make_sale(county="Montgomery", municipality="Lower Merion", zip_code="19035")
    kept, _ = filter_sales([chester, montco], config, today=today)
    assert [sale.area for sale in kept] == ["Chester County", "Montgomery County"]


def test_area_match_works_from_zip_alone(config):
    """News items rarely name the county; the ZIP still places them."""
    sale = make_sale(county=None, municipality=None, zip_code="19085")
    kept, _ = filter_sales([sale], config, today=date(2026, 3, 20))
    assert kept and kept[0].area == "Radnor Township"
