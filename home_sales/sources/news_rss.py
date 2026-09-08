"""RSS/Atom source for local "notable sales" coverage.

Coverage here is incomplete and the extraction is heuristic -- news articles are
prose, not records. It exists to catch high-end sales that appear in press
before they clear the recorder of deeds, so it should be ranked *below* county
sources: when both describe the same transaction, the county record wins on
price and date.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Iterable, Iterator
from xml.etree import ElementTree

from ..models import Sale, build_sale
from ..normalize import normalize_municipality
from .base import ConfigurationError, Source, register

log = logging.getLogger(__name__)

_STREET_SUFFIXES = (
    "Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|Circle|Cir|Boulevard|Blvd|"
    "Place|Pl|Terrace|Ter|Way|Trail|Trl|Pike|Parkway|Pkwy|Square|Sq|Row|Run|Crescent"
)

_ADDRESS_RE = re.compile(
    rf"\b(\d{{1,6}}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z'\-\.]*\s+){{0,4}}(?:{_STREET_SUFFIXES})\b\.?)",
)

_PRICE_RE = re.compile(
    r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(million|mil|m|k|thousand)?\b",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")

_ATOM = "{http://www.w3.org/2005/Atom}"


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).replace("\xa0", " ")


def extract_price(text: str) -> int | None:
    """Return the largest plausible sale price mentioned.

    Articles mention several figures (list price, prior sale, taxes); the sale
    price is reliably the largest, and anything under $100k is not a $1M+ sale
    reference worth keeping.
    """
    best: int | None = None
    for match in _PRICE_RE.finditer(text or ""):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        if suffix in ("million", "mil", "m"):
            amount *= 1_000_000
        elif suffix in ("k", "thousand"):
            amount *= 1_000
        value = int(round(amount))
        if value >= 100_000 and (best is None or value > best):
            best = value
    return best


def extract_address(text: str) -> str | None:
    match = _ADDRESS_RE.search(text or "")
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip(" .")


@register("news_rss")
class NewsRssSource(Source):
    def fetch(self) -> Iterable[Sale]:
        feeds = self.option("feeds", required=True)
        if isinstance(feeds, str):
            feeds = [feeds]

        known_municipalities = self._known_municipalities()
        count = 0
        for feed_url in feeds:
            try:
                document = self.fetcher.get(str(feed_url)).text
            except Exception as exc:  # one bad feed shouldn't kill the source
                log.warning("%s: feed %s failed: %s", self.name, feed_url, exc)
                continue

            for entry in self._iter_entries(document):
                sale = self._to_sale(entry, known_municipalities)
                if sale is not None:
                    count += 1
                    yield sale
        log.info("%s: produced %d sales", self.name, count)

    def _known_municipalities(self) -> dict[str, str]:
        """Municipality names we can recognize in prose, longest first."""
        names: dict[str, str] = {}
        for area in self.config.areas:
            for municipality in area.municipalities:
                names[normalize_municipality(municipality)] = municipality
        for extra in self.option("municipalities", []) or []:
            names[normalize_municipality(str(extra))] = str(extra)
        return names

    def _iter_entries(self, document: str) -> Iterator[dict[str, Any]]:
        try:
            root = ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise ConfigurationError(f"{self.name}: feed is not valid XML: {exc}") from None

        for item in root.iter("item"):  # RSS 2.0
            yield {
                "title": (item.findtext("title") or "").strip(),
                "summary": strip_html(item.findtext("description") or ""),
                "link": (item.findtext("link") or "").strip(),
                "date": item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date"),
            }

        for entry in root.iter(f"{_ATOM}entry"):  # Atom
            link_element = entry.find(f"{_ATOM}link")
            yield {
                "title": (entry.findtext(f"{_ATOM}title") or "").strip(),
                "summary": strip_html(
                    entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or ""
                ),
                "link": (link_element.get("href") if link_element is not None else "") or "",
                "date": entry.findtext(f"{_ATOM}updated") or entry.findtext(f"{_ATOM}published"),
            }

    def _to_sale(self, entry: dict[str, Any], municipalities: dict[str, str]) -> Sale | None:
        text = f"{entry.get('title', '')}. {entry.get('summary', '')}"
        address = extract_address(text)
        price = extract_price(text)
        if not address or price is None:
            return None

        municipality = None
        normalized_text = normalize_municipality(text)
        for normalized_name, display in sorted(
            municipalities.items(), key=lambda pair: -len(pair[0])
        ):
            if normalized_name and re.search(rf"\b{re.escape(normalized_name)}\b", normalized_text):
                municipality = display
                break

        published = _parse_feed_date(entry.get("date"))
        return build_sale(
            address=address,
            price=price,
            sale_date=published,
            source=self.name,
            municipality=municipality,
            county=self.option("county"),
            source_url=entry.get("link") or None,
            raw={"title": entry.get("title"), "summary": entry.get("summary")},
        )


def _parse_feed_date(value: Any) -> Any:
    """Feed dates are RFC 822; fall back to the shared parser for everything else."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(str(value)).date()
    except (TypeError, ValueError):
        from ..normalize import parse_date

        return parse_date(value)
