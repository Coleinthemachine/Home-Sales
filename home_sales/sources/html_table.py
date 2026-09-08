"""Generic HTML source for sites that publish sales as tables or card lists.

Configured with CSS selectors so a new site is a config block. Requests go
through the shared Fetcher, which enforces robots.txt -- if a site disallows
crawling, this source raises rather than working around the restriction. For
Zillow, Redfin and Realtor.com that is the expected outcome: their robots.txt
and terms forbid automated collection, so use a licensed API (the `json_api`
source) or county records for that coverage instead.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator

from bs4 import BeautifulSoup

from ..models import Sale, build_sale
from .base import ConfigurationError, Source, register

log = logging.getLogger(__name__)


@register("html_table")
class HtmlTableSource(Source):
    def fetch(self) -> Iterable[Sale]:
        url_template = str(self.option("url", required=True))
        row_selector = str(self.option("row_selector", required=True))
        cell_map: dict[str, str] = dict(self.option("cell_map", {}) or {})
        for required_key in ("address", "price", "sale_date"):
            if required_key not in cell_map:
                raise ConfigurationError(f"Source {self.name!r} needs cell_map.{required_key}.")

        max_pages = int(self.option("max_pages", 1))
        page_start = int(self.option("page_start", 1))

        count = 0
        for page in range(page_start, page_start + max_pages):
            url = url_template.replace("{page}", str(page))
            document = self.fetcher.get(url).text
            rows = list(self._iter_rows(document, row_selector, cell_map, url))
            if not rows:
                break
            for sale in rows:
                count += 1
                yield sale
            if "{page}" not in url_template:
                break
        log.info("%s: produced %d sales", self.name, count)

    def _iter_rows(
        self, document: str, row_selector: str, cell_map: dict[str, str], url: str
    ) -> Iterator[Sale]:
        soup = BeautifulSoup(document, "html.parser")
        for row in soup.select(row_selector):
            values: dict[str, Any] = {}
            for target, selector in cell_map.items():
                cell = row.select_one(str(selector))
                if cell is None:
                    continue
                text = cell.get_text(" ", strip=True)
                if text:
                    values[target] = text
                if target == "source_url" and cell.has_attr("href"):
                    values[target] = cell["href"]

            address = values.pop("address", None)
            if not address:
                continue

            values.setdefault("county", self.option("county"))
            values.setdefault("municipality", self.option("municipality"))
            values = {key: value for key, value in values.items() if value is not None}

            sale = build_sale(
                address=str(address),
                price=values.pop("price", None),
                sale_date=values.pop("sale_date", None),
                source=self.name,
                source_url=values.pop("source_url", None) or url,
                raw={"row_text": row.get_text(" ", strip=True)[:500]},
                **values,
            )
            if sale is not None:
                yield sale
