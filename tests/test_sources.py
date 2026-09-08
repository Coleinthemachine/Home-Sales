import json
from datetime import date, timedelta

import pytest

from home_sales.config import SourceConfig
from home_sales.sources import ConfigurationError, build_source
from home_sales.sources.json_api import dig
from home_sales.sources.news_rss import extract_address, extract_price


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeFetcher:
    """Records requests and replays canned payloads keyed by URL substring."""

    def __init__(self, payloads: dict[str, object]):
        self.payloads = payloads
        self.calls: list[tuple[str, dict]] = []

    def _lookup(self, url: str):
        for key, payload in self.payloads.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected request to {url}")

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self._lookup(url)
        return FakeResponse(payload if isinstance(payload, str) else json.dumps(payload))

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self._lookup(url)
        return json.loads(payload) if isinstance(payload, str) else payload


def build(config, name, type_name, options, fetcher):
    spec = SourceConfig(name=name, type=type_name, options=options)
    return build_source(spec, config, fetcher)


# --- ArcGIS ---------------------------------------------------------------

ARCGIS_OPTIONS = {
    "service_url": "https://gis.example.org/arcgis/rest/services/Parcels/FeatureServer/0",
    "price_field": "SALE_PRICE",
    "date_field": "SALE_DATE",
    "county": "Delaware",
    "field_map": {
        "address": "PROP_ADDR",
        "municipality": "MUNI",
        "zip_code": "ZIP",
        "parcel_id": "PARCEL",
        "buyer": "GRANTEE",
        "seller": "GRANTOR",
    },
}


def arcgis_payload(features, exceeded=False):
    return {"features": features, "exceededTransferLimit": exceeded}


def test_arcgis_maps_fields_and_epoch_dates(config):
    fetcher = FakeFetcher(
        {
            "/query": arcgis_payload(
                [
                    {
                        "attributes": {
                            "PROP_ADDR": "123 N WAYNE AVE",
                            "MUNI": "RADNOR TWP",
                            "ZIP": "19087",
                            "PARCEL": "36-01-0042",
                            "GRANTEE": "SMITH JOHN",
                            "GRANTOR": "JONES MARY",
                            "SALE_PRICE": 1750000,
                            "SALE_DATE": 1772323200000,
                        }
                    }
                ]
            )
        }
    )
    sales = list(build(config, "delco", "arcgis", ARCGIS_OPTIONS, fetcher).fetch())

    assert len(sales) == 1
    sale = sales[0]
    assert sale.price == 1_750_000
    assert sale.sale_date == date(2026, 3, 1)
    assert sale.municipality == "RADNOR TWP"
    assert sale.county == "Delaware"
    assert sale.parcel_id == "36-01-0042"
    assert sale.buyer == "SMITH JOHN"
    assert sale.raw["PARCEL"] == "36-01-0042"


def test_arcgis_where_clause_includes_price_and_date_floor(config):
    fetcher = FakeFetcher({"/query": arcgis_payload([])})
    list(build(config, "delco", "arcgis", ARCGIS_OPTIONS, fetcher).fetch())

    where = fetcher.calls[0][1]["params"]["where"]
    assert "SALE_PRICE >= 1000000" in where
    assert "SALE_DATE >= date '" in where


def test_arcgis_epoch_literal_mode(config):
    options = {**ARCGIS_OPTIONS, "date_literal": "epoch_ms"}
    fetcher = FakeFetcher({"/query": arcgis_payload([])})
    list(build(config, "delco", "arcgis", options, fetcher).fetch())

    where = fetcher.calls[0][1]["params"]["where"]
    assert "SALE_DATE >= 1" in where and "date '" not in where


def test_arcgis_paginates_until_exhausted(config):
    page_one = arcgis_payload(
        [
            {"attributes": {"PROP_ADDR": f"{n} Elm St", "SALE_PRICE": 1_200_000,
                            "SALE_DATE": "2026-03-01", "MUNI": "RADNOR"}}
            for n in range(1000)
        ],
        exceeded=True,
    )
    page_two = arcgis_payload(
        [{"attributes": {"PROP_ADDR": "9999 Elm St", "SALE_PRICE": 1_200_000,
                         "SALE_DATE": "2026-03-01", "MUNI": "RADNOR"}}]
    )

    class Paging(FakeFetcher):
        def get_json(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return page_one if kwargs["params"]["resultOffset"] == 0 else page_two

    fetcher = Paging({})
    sales = list(build(config, "delco", "arcgis", ARCGIS_OPTIONS, fetcher).fetch())
    assert len(sales) == 1001
    assert len(fetcher.calls) == 2


def test_arcgis_surfaces_server_errors(config):
    fetcher = FakeFetcher(
        {"/query": {"error": {"message": "Invalid field: SALE_PRICE", "details": ["bad field"]}}}
    )
    with pytest.raises(ConfigurationError, match="Invalid field"):
        list(build(config, "delco", "arcgis", ARCGIS_OPTIONS, fetcher).fetch())


def test_arcgis_requires_address_mapping(config):
    options = {**ARCGIS_OPTIONS, "field_map": {"municipality": "MUNI"}}
    with pytest.raises(ConfigurationError, match="field_map.address"):
        list(build(config, "delco", "arcgis", options, FakeFetcher({})).fetch())


def test_arcgis_rejects_injected_field_name(config):
    options = {**ARCGIS_OPTIONS, "price_field": "PRICE; DROP TABLE parcels--"}
    with pytest.raises(ConfigurationError, match="plain ArcGIS field name"):
        list(build(config, "delco", "arcgis", options, FakeFetcher({})).fetch())


# --- JSON API -------------------------------------------------------------


def test_dig_walks_nested_paths():
    payload = {"a": {"b": [{"c": 7}]}}
    assert dig(payload, "a.b.0.c") == 7
    assert dig(payload, "a.missing.c") is None
    assert dig(payload, "") is payload


def test_json_api_maps_nested_fields(config):
    options = {
        "url": "https://api.example.com/sales",
        "records_path": "property",
        "field_map": {
            "address": "address.line1",
            "price": "sale.amount.saleamt",
            "sale_date": "sale.salesearchdate",
            "municipality": "address.locality",
            "zip_code": "address.postal1",
            "sqft": "building.size.livingsize",
        },
        "headers": {"apikey": "test-key"},
    }
    fetcher = FakeFetcher(
        {
            "api.example.com": {
                "property": [
                    {
                        "address": {"line1": "9 Old Gulph Rd", "locality": "Villanova", "postal1": "19085"},
                        "sale": {"amount": {"saleamt": 2450000}, "salesearchdate": "2026-03-05"},
                        "building": {"size": {"livingsize": 5200}},
                    }
                ]
            }
        }
    )
    sales = list(build(config, "attom", "json_api", options, fetcher).fetch())

    assert len(sales) == 1
    assert sales[0].price == 2_450_000
    assert sales[0].municipality == "Villanova"
    assert sales[0].sqft == 5200
    assert fetcher.calls[0][1]["headers"] == {"apikey": "test-key"}


def test_json_api_substitutes_runtime_placeholders(config):
    options = {
        "url": "https://api.example.com/sales",
        "params": {"minPrice": "{min_price}", "from": "{since}"},
        "field_map": {"address": "addr", "price": "price", "sale_date": "date"},
    }
    fetcher = FakeFetcher({"api.example.com": []})
    list(build(config, "api", "json_api", options, fetcher).fetch())

    expected_since = (date.today() - timedelta(days=config.lookback_days)).isoformat()
    params = fetcher.calls[0][1]["params"]
    assert params["minPrice"] == "1000000"
    assert params["from"] == expected_since


def test_json_api_refuses_unexpanded_api_key(config):
    options = {
        "url": "https://api.example.com/sales",
        "headers": {"X-Api-Key": "${RENTCAST_API_KEY}"},
        "field_map": {"address": "a", "price": "p", "sale_date": "d"},
    }
    with pytest.raises(ConfigurationError, match="is unset"):
        list(build(config, "rentcast", "json_api", options, FakeFetcher({})).fetch())


def test_json_api_reports_bad_records_path(config):
    options = {
        "url": "https://api.example.com/sales",
        "records_path": "data",
        "field_map": {"address": "a", "price": "p", "sale_date": "d"},
    }
    fetcher = FakeFetcher({"api.example.com": {"data": "not-a-list"}})
    with pytest.raises(ConfigurationError, match="records_path"):
        list(build(config, "api", "json_api", options, fetcher).fetch())


# --- News RSS -------------------------------------------------------------


def test_extract_price_takes_largest_figure():
    text = "Listed at $1.9 million, it last sold for $780,000 in 2011 and closed at $2,150,000."
    assert extract_price(text) == 2_150_000


def test_extract_price_handles_suffixes():
    assert extract_price("sold for $1.85 million") == 1_850_000
    assert extract_price("sold for $2.4M") == 2_400_000


def test_extract_price_ignores_small_figures():
    """Tax and fee amounts shouldn't be mistaken for a sale price."""
    assert extract_price("annual taxes of $12,000") is None
    assert extract_price("no dollar figures at all") is None


def test_extract_address_finds_street():
    assert extract_address("The home at 123 N Wayne Ave sold Tuesday") == "123 N Wayne Ave"
    assert extract_address("No address in this sentence") is None


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Villanova estate at 9 Old Gulph Road sells for $2.4 million</title>
      <description>The Radnor Township property closed last week.</description>
      <link>https://news.example.com/story/1</link>
      <pubDate>Wed, 04 Mar 2026 12:00:00 -0500</pubDate>
    </item>
    <item>
      <title>Local market roundup</title>
      <description>No specific sale mentioned here.</description>
      <link>https://news.example.com/story/2</link>
      <pubDate>Wed, 04 Mar 2026 12:00:00 -0500</pubDate>
    </item>
  </channel>
</rss>
"""


def test_news_rss_extracts_sale_and_skips_prose(config):
    options = {"feeds": ["https://news.example.com/feed"], "county": "Delaware"}
    fetcher = FakeFetcher({"news.example.com": RSS_FIXTURE})
    sales = list(build(config, "news", "news_rss", options, fetcher).fetch())

    assert len(sales) == 1
    assert sales[0].price == 2_400_000
    assert sales[0].sale_date == date(2026, 3, 4)
    # Both "Villanova" and "Radnor Township" appear; the longer, more specific
    # place name wins. Either resolves to the Radnor Township area downstream.
    assert sales[0].municipality == "Villanova"
    assert sales[0].source_url == "https://news.example.com/story/1"


ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>500 Lancaster Avenue trades at $1.6 million</title>
    <summary>A Wayne landmark changed hands.</summary>
    <link href="https://news.example.com/atom/1"/>
    <updated>2026-03-02T10:00:00Z</updated>
  </entry>
</feed>
"""


def test_news_rss_reads_atom(config):
    fetcher = FakeFetcher({"news.example.com": ATOM_FIXTURE})
    sales = list(
        build(config, "news", "news_rss", {"feeds": ["https://news.example.com/feed"]}, fetcher).fetch()
    )
    assert len(sales) == 1
    assert sales[0].price == 1_600_000
    assert sales[0].municipality == "Wayne"


def test_news_rss_survives_one_bad_feed(config):
    options = {"feeds": ["https://bad.example.com/feed", "https://news.example.com/feed"]}

    class Flaky(FakeFetcher):
        def get(self, url, **kwargs):
            if "bad.example.com" in url:
                raise RuntimeError("connection reset")
            return super().get(url, **kwargs)

    sales = list(build(config, "news", "news_rss", options, Flaky({"news.example.com": RSS_FIXTURE})).fetch())
    assert len(sales) == 1


# --- HTML tables ----------------------------------------------------------

HTML_FIXTURE = """
<html><body>
<table class="transfers"><tbody>
  <tr><td>15 Coopertown Rd</td><td>03/02/2026</td><td>$1,425,000</td><td>Haverford</td></tr>
  <tr><td>2 Maple Ave</td><td>03/03/2026</td><td>$980,000</td><td>Wayne</td></tr>
  <tr><td></td><td>03/04/2026</td><td>$3,000,000</td><td>Wayne</td></tr>
</tbody></table>
</body></html>
"""


def test_html_table_parses_rows(config):
    options = {
        "url": "https://county.example.org/transfers",
        "row_selector": "table.transfers tbody tr",
        "county": "Delaware",
        "cell_map": {
            "address": "td:nth-child(1)",
            "sale_date": "td:nth-child(2)",
            "price": "td:nth-child(3)",
            "municipality": "td:nth-child(4)",
        },
    }
    fetcher = FakeFetcher({"county.example.org": HTML_FIXTURE})
    sales = list(build(config, "transfers", "html_table", options, fetcher).fetch())

    # The row with no address is unusable and dropped; price filtering is
    # downstream, so the $980k row still parses here.
    assert [sale.price for sale in sales] == [1_425_000, 980_000]
    assert sales[0].municipality == "Haverford"
    assert sales[0].sale_date == date(2026, 3, 2)
