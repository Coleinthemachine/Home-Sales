from datetime import date, datetime, timedelta, timezone

import pytest

from home_sales.models import Sale
from home_sales.store import Store
from home_sales.web import create_app

CONFIG_TEMPLATE = """
[filters]
min_price = 1000000
lookback_days = 45

[storage]
database = "{database}"

[fetching]
user_agent = "test-bot/1.0"

[[areas]]
name = "Radnor Township"
counties = ["Delaware"]
municipalities = ["Radnor", "Wayne", "Villanova"]
zips = ["19087", "19085"]

[[areas]]
name = "Montgomery County"
counties = ["Montgomery"]

[sources.county]
type = "arcgis"
enabled = false
rank = 90
service_url = "https://example.org/FeatureServer/0"
price_field = "SALE_PRICE"
date_field = "SALE_DATE"

[sources.county.field_map]
address = "ADDR"
"""


@pytest.fixture
def app_and_store(tmp_path):
    database = tmp_path / "web.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TEMPLATE.format(database=database.as_posix()))

    with Store(database) as store:
        recent = date.today() - timedelta(days=5)
        store.upsert(
            Sale(address="123 N Wayne Ave", price=1_750_000, sale_date=recent, source="county",
                 county="Delaware", municipality="Radnor Township", zip_code="19087",
                 area="Radnor Township", buyer="Smith John", beds=5, sqft=4200),
            {"county": 90},
        )
        store.upsert(
            Sale(address="9 Old Gulph Rd", price=3_400_000, sale_date=recent, source="county",
                 county="Montgomery", municipality="Lower Merion", zip_code="19035",
                 area="Montgomery County"),
            {"county": 90},
        )
        store.record_source_run(
            "run1", "zillow", "blocked", datetime.now(timezone.utc), 0, 0, "robots.txt disallows"
        )

    app = create_app(config_path)
    app.config.update(TESTING=True)
    return app


def test_index_lists_sales(app_and_store):
    client = app_and_store.test_client()
    body = client.get("/").get_data(as_text=True)

    assert "123 North Wayne Ave" in body or "123 N Wayne Ave" in body
    assert "$1,750,000" in body
    assert "$3,400,000" in body
    assert "Radnor Township" in body


def test_area_filter(app_and_store):
    body = app_and_store.test_client().get("/?area=Montgomery County").get_data(as_text=True)
    assert "$3,400,000" in body
    assert "$1,750,000" not in body


def test_min_price_filter(app_and_store):
    body = app_and_store.test_client().get("/?min_price=2000000").get_data(as_text=True)
    assert "$3,400,000" in body
    assert "$1,750,000" not in body


def test_search_filter(app_and_store):
    body = app_and_store.test_client().get("/?q=Gulph").get_data(as_text=True)
    assert "$3,400,000" in body
    assert "$1,750,000" not in body


def test_date_window_excludes_old_sales(app_and_store):
    body = app_and_store.test_client().get("/?days=1").get_data(as_text=True)
    assert "No sales match these filters" in body


def test_invalid_filter_values_fall_back_to_defaults(app_and_store):
    response = app_and_store.test_client().get("/?days=abc&min_price=xyz")
    assert response.status_code == 200


def test_source_health_is_surfaced(app_and_store):
    body = app_and_store.test_client().get("/").get_data(as_text=True)
    assert "Source health" in body
    assert "blocked" in body
    assert "robots.txt disallows" in body


def test_csv_export(app_and_store):
    response = app_and_store.test_client().get("/export.csv")
    text = response.get_data(as_text=True)

    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert "sale_date,price,address" in text
    assert "1750000" in text
    assert "Smith John" in text


def test_csv_export_respects_filters(app_and_store):
    text = app_and_store.test_client().get("/export.csv?area=Montgomery County").get_data(as_text=True)
    assert "3400000" in text
    assert "1750000" not in text


def test_healthz(app_and_store):
    payload = app_and_store.test_client().get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["tracked_sales"] == 2
