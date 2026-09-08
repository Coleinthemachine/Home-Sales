"""Generic ArcGIS REST source for county parcel/sales layers.

PA counties publish assessment and deed-transfer data as ArcGIS feature
services. They are public, stable and intended for programmatic use, which
makes them the right primary source. Every county differs in layer URL and
field naming, so all of that is configuration -- see `discover_service` in
home_sales/discovery.py for finding the right values.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Iterator

from ..models import Sale, build_sale
from .base import ConfigurationError, Source, register

log = logging.getLogger(__name__)

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PAGE_SIZE = 1000
_MAX_PAGES = 200

# Sale records commonly carry these; anything else the layer offers is kept in raw.
_OPTIONAL_FIELDS = (
    "municipality",
    "zip_code",
    "county",
    "parcel_id",
    "buyer",
    "seller",
    "beds",
    "baths",
    "sqft",
    "lot_acres",
    "year_built",
    "property_type",
    "unit",
)


def _validate_field(name: str, label: str) -> str:
    if not _FIELD_RE.match(name or ""):
        raise ConfigurationError(
            f"{label} must be a plain ArcGIS field name, got {name!r}."
        )
    return name


@register("arcgis")
class ArcGISSource(Source):
    def fetch(self) -> Iterable[Sale]:
        service_url = str(self.option("service_url", required=True)).rstrip("/")
        price_field = _validate_field(self.option("price_field", required=True), "price_field")
        date_field = _validate_field(self.option("date_field", required=True), "date_field")
        field_map: dict[str, str] = dict(self.option("field_map", {}) or {})
        if "address" not in field_map:
            raise ConfigurationError(
                f"Source {self.name!r} needs field_map.address -- the layer's street address column."
            )

        where = self._build_where(price_field, date_field)
        want_geometry = bool(self.option("return_geometry", False))

        yielded = 0
        for feature in self._iter_features(service_url, where, want_geometry):
            sale = self._to_sale(feature, price_field, date_field, field_map, service_url)
            if sale is not None:
                yielded += 1
                yield sale
        log.info("%s: produced %d sales", self.name, yielded)

    def _build_where(self, price_field: str, date_field: str) -> str:
        clauses = [f"{price_field} >= {int(self.config.min_price)}"]

        literal = str(self.option("date_literal", "date")).lower()
        since = self.since.isoformat()
        if literal == "timestamp":
            clauses.append(f"{date_field} >= timestamp '{since} 00:00:00'")
        elif literal == "string":
            clauses.append(f"{date_field} >= '{since}'")
        elif literal == "epoch_ms":
            from datetime import datetime, timezone

            epoch = int(
                datetime(self.since.year, self.since.month, self.since.day, tzinfo=timezone.utc).timestamp()
                * 1000
            )
            clauses.append(f"{date_field} >= {epoch}")
        else:
            clauses.append(f"{date_field} >= date '{since}'")

        extra = self.option("where")
        if extra:
            clauses.append(f"({extra})")
        return " AND ".join(clauses)

    def _iter_features(
        self, service_url: str, where: str, want_geometry: bool
    ) -> Iterator[dict[str, Any]]:
        offset = 0
        for page in range(_MAX_PAGES):
            params = {
                "where": where,
                "outFields": "*",
                "returnGeometry": "true" if want_geometry else "false",
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
                "f": "json",
            }
            if want_geometry:
                params["outSR"] = "4326"

            payload = self.fetcher.get_json(f"{service_url}/query", params=params)
            if isinstance(payload, dict) and "error" in payload:
                error = payload["error"]
                raise ConfigurationError(
                    f"{self.name}: ArcGIS returned an error for {service_url}: "
                    f"{error.get('message')} {'; '.join(error.get('details', []))}".strip()
                )

            features = (payload or {}).get("features") or []
            if not features:
                return
            yield from features

            if not payload.get("exceededTransferLimit") and len(features) < _PAGE_SIZE:
                return
            offset += len(features)
        log.warning("%s: stopped after %d pages", self.name, _MAX_PAGES)

    def _to_sale(
        self,
        feature: dict[str, Any],
        price_field: str,
        date_field: str,
        field_map: dict[str, str],
        service_url: str,
    ) -> Sale | None:
        attributes = feature.get("attributes") or {}
        if not attributes:
            return None

        address_field = field_map["address"]
        address = attributes.get(address_field)
        if not address:
            return None

        kwargs: dict[str, Any] = {}
        for target in _OPTIONAL_FIELDS:
            source_field = field_map.get(target)
            if source_field and attributes.get(source_field) not in (None, ""):
                kwargs[target] = attributes[source_field]

        # A layer scoped to one county usually doesn't repeat it per row.
        kwargs.setdefault("county", self.option("county"))
        kwargs.setdefault("municipality", self.option("municipality"))

        geometry = feature.get("geometry") or {}
        if isinstance(geometry.get("y"), (int, float)):
            kwargs["latitude"] = geometry["y"]
            kwargs["longitude"] = geometry["x"]

        return build_sale(
            address=str(address),
            price=attributes.get(price_field),
            sale_date=attributes.get(date_field),
            source=self.name,
            source_url=self.option("record_url") or service_url,
            raw=attributes,
            **kwargs,
        )
