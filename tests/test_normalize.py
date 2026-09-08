from datetime import date

import pytest

from home_sales import normalize


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("123 North Wayne Avenue", "123 N WAYNE AVE"),
        ("123 N. Wayne Ave.", "123 N WAYNE AVE"),
        ("  123   n wayne   avenue  ", "123 N WAYNE AVE"),
        ("45 Second Street", "45 2ND ST"),
        ("45 2nd St", "45 2ND ST"),
        ("800 King of Prussia Road", "800 KING OF PRUSSIA RD"),
    ],
)
def test_normalize_address_collapses_variants(raw, expected):
    assert normalize.normalize_address(raw) == expected


def test_normalize_address_strips_unit():
    assert normalize.normalize_address("100 Main St Apt 4B") == "100 MAIN ST"
    assert normalize.normalize_address("100 Main St #4B") == "100 MAIN ST"


@pytest.mark.parametrize(
    "raw, base, unit",
    [
        ("100 Main St Apt 4B", "100 MAIN ST", "4B"),
        ("100 Main St #12", "100 MAIN ST", "12"),
        ("100 Main St, Unit 7", "100 MAIN ST,", "7"),
        ("100 Main St", "100 MAIN ST", None),
    ],
)
def test_split_unit(raw, base, unit):
    assert normalize.split_unit(raw) == (base, unit)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Radnor Township", "RADNOR"),
        ("RADNOR TWP", "RADNOR"),
        ("Radnor", "RADNOR"),
        ("Lower Merion Township", "LOWER MERION"),
        ("West Chester Borough", "WEST CHESTER"),
        ("City of Chester", "CHESTER"),
        ("Chester City", "CHESTER"),
    ],
)
def test_normalize_municipality(raw, expected):
    assert normalize.normalize_municipality(raw) == expected


def test_normalize_county_drops_suffix():
    assert normalize.normalize_county("Delaware County") == "DELAWARE"
    assert normalize.normalize_county("DELAWARE") == "DELAWARE"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$1,250,000", 1_250_000),
        ("1250000", 1_250_000),
        ("1250000.00", 1_250_000),
        (1_250_000, 1_250_000),
        (1_250_000.49, 1_250_000),
        ("$1.85 million", 1_850_000),
        ("1.85M", 1_850_000),
        ("$950K", 950_000),
        ("", None),
        (None, None),
        ("no price here", None),
        (0, None),
        (-5, None),
    ],
)
def test_parse_price(raw, expected):
    assert normalize.parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-03-14", date(2026, 3, 14)),
        ("03/14/2026", date(2026, 3, 14)),
        ("March 14, 2026", date(2026, 3, 14)),
        ("2026-03-14T00:00:00Z", date(2026, 3, 14)),
        (date(2026, 3, 14), date(2026, 3, 14)),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_parse_date(raw, expected):
    assert normalize.parse_date(raw) == expected


def test_parse_date_handles_arcgis_epoch_millis():
    assert normalize.parse_date(1_773_446_400_000) == date(2026, 3, 14)


def test_normalize_zip():
    assert normalize.normalize_zip("19087") == "19087"
    assert normalize.normalize_zip("19087-1234") == "19087"
    assert normalize.normalize_zip(19087) == "19087"
    assert normalize.normalize_zip("PA") is None


def test_property_key_is_stable_across_formatting():
    left = normalize.property_key("123 North Wayne Avenue", "19087", "Radnor Township")
    right = normalize.property_key("123 N. Wayne Ave.", "19087-0000", "RADNOR TWP")
    assert left == right


def test_property_key_separates_units():
    base = normalize.property_key("500 Lancaster Ave Apt 1", "19010", None)
    other = normalize.property_key("500 Lancaster Ave Apt 2", "19010", None)
    assert base != other


def test_property_key_falls_back_to_municipality_without_zip():
    left = normalize.property_key("1 Elm St", None, "Radnor Township")
    right = normalize.property_key("1 ELM STREET", None, "RADNOR")
    assert left == right
