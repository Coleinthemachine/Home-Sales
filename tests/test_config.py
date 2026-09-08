import pytest

from home_sales.config import Area, load_config

FULL_CONFIG = """
[filters]
min_price = 1500000
lookback_days = 30
dedupe_window_days = 7

[storage]
database = "custom/path.db"

[fetching]
user_agent = "my-bot/2.0"
contact_email = "me@example.com"
request_delay_seconds = 2.5
respect_robots = false

[[areas]]
name = "Radnor Township"
counties = ["Delaware"]
municipalities = ["Radnor", "Wayne"]
zips = ["19087"]

[[areas]]
name = "Chester County"
counties = ["Chester"]

[sources.county]
type = "arcgis"
rank = 90
service_url = "https://example.org/FeatureServer/0"
price_field = "SALE_PRICE"
date_field = "SALE_DATE"

[sources.county.field_map]
address = "ADDR"

[sources.vendor]
type = "json_api"
enabled = false
rank = 50
url = "https://api.example.com/v1/sales"

[sources.vendor.headers]
"X-Api-Key" = "${TEST_VENDOR_KEY}"
"""


def write_config(tmp_path, text=FULL_CONFIG):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_loads_scalar_settings(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.min_price == 1_500_000
    assert config.lookback_days == 30
    assert config.dedupe_window_days == 7
    assert config.user_agent == "my-bot/2.0"
    assert config.request_delay_seconds == 2.5
    assert config.respect_robots is False


def test_database_path_is_read_from_storage_table(tmp_path):
    """Regression: a bare `database` key after [filters] silently lands inside
    that table, so it must live under its own [storage] table."""
    config = load_config(write_config(tmp_path))
    assert config.database == "custom/path.db"


def test_missing_config_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="--config"):
        load_config(tmp_path / "nope.toml")


def test_areas_and_sources_are_parsed(tmp_path):
    config = load_config(write_config(tmp_path))

    assert [area.name for area in config.areas] == ["Radnor Township", "Chester County"]
    assert config.areas[0].zips == ("19087",)

    names = {source.name: source for source in config.sources}
    assert names["county"].type == "arcgis"
    assert names["county"].rank == 90
    assert names["county"].options["field_map"] == {"address": "ADDR"}
    assert names["vendor"].enabled is False
    assert [s.name for s in config.enabled_sources()] == ["county"]
    assert config.source_ranks == {"county": 90, "vendor": 50}


def test_env_vars_expand_in_source_options(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VENDOR_KEY", "secret-123")
    config = load_config(write_config(tmp_path))
    vendor = next(s for s in config.sources if s.name == "vendor")
    assert vendor.options["headers"]["X-Api-Key"] == "secret-123"


def test_unset_env_var_stays_literal_so_the_source_can_refuse(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_VENDOR_KEY", raising=False)
    config = load_config(write_config(tmp_path))
    vendor = next(s for s in config.sources if s.name == "vendor")
    assert vendor.options["headers"]["X-Api-Key"] == "${TEST_VENDOR_KEY}"


def test_shipped_config_is_valid():
    """The committed config must load -- the weekly workflow depends on it."""
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    assert config.min_price == 1_000_000
    assert config.database == "data/home_sales.db"
    assert {area.name for area in config.areas} == {
        "Radnor Township", "Chester County", "Montgomery County", "Delaware County"
    }
    # Everything ships disabled: endpoints must be verified before first run.
    assert config.enabled_sources() == []


# --- Area matching --------------------------------------------------------


def test_county_area_claims_whole_county():
    area = Area(name="Chester County", counties=("Chester",))
    assert area.matches("CHESTER", "WEST CHESTER", "19380")
    assert not area.matches("DELAWARE", "MEDIA", "19063")


def test_sub_county_area_requires_municipality_or_zip():
    """Listing Delaware as a county must not make Radnor claim all of Delco."""
    area = Area(
        name="Radnor Township",
        counties=("Delaware",),
        municipalities=("Radnor", "Wayne"),
        zips=("19087",),
    )
    assert area.matches("DELAWARE", "RADNOR", None)
    assert area.matches("DELAWARE", "", "19087")
    assert not area.matches("DELAWARE", "MEDIA", "19063")


def test_match_area_prefers_the_specific_area(tmp_path):
    config = load_config(write_config(tmp_path))
    config.areas.append(Area(name="Delaware County", counties=("Delaware",)))

    assert config.match_area("DELAWARE", "RADNOR", "19087").name == "Radnor Township"
    assert config.match_area("DELAWARE", "MEDIA", "19063").name == "Delaware County"
    assert config.match_area("BUCKS", "DOYLESTOWN", "18901") is None
