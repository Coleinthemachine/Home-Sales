"""ArcGIS service inspection, to turn an unknown county endpoint into config.

County GIS portals rename layers and fields without notice, and every county
names its columns differently (SALE_PRICE, SalePrice, CONSIDERATION, PRICE).
Rather than hardcode guesses, this walks a service and reports what is actually
there, then proposes the `[sources.*]` block to paste into config.toml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .http import Fetcher

# Ordered best-guess first; scoring prefers earlier, more specific patterns.
_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "price": ("sale_price", "saleprice", "sales_price", "consideration", "amount", "price"),
    "sale_date": ("sale_date", "saledate", "deed_date", "transfer_date", "recorded", "date"),
    "address": ("prop_addr", "situs", "site_address", "property_address", "full_address", "address", "addr", "location"),
    "municipality": ("municipality", "muni", "township", "boro", "city", "town", "place"),
    "zip_code": ("zip", "postal"),
    "county": ("county",),
    "parcel_id": ("parcel", "pin", "apn", "folio", "uid", "map_no", "tax_id"),
    "buyer": ("grantee", "buyer", "owner_new", "new_owner"),
    "seller": ("grantor", "seller", "owner_prev", "prior_owner"),
    "beds": ("bed",),
    "baths": ("bath",),
    "sqft": ("sq_ft", "sqft", "square_feet", "living_area", "finished_area"),
    "lot_acres": ("acre", "lot_size"),
    "year_built": ("year_built", "yearbuilt", "yr_built", "actual_year"),
    "property_type": ("land_use", "use_code", "property_type", "class", "descr"),
}

_NUMERIC_TYPES = {
    "esriFieldTypeDouble",
    "esriFieldTypeSingle",
    "esriFieldTypeInteger",
    "esriFieldTypeSmallInteger",
    "esriFieldTypeBigInteger",
}
_DATE_TYPES = {"esriFieldTypeDate", "esriFieldTypeDateOnly", "esriFieldTypeTimestampOffset"}
_TEXT_TYPES = {"esriFieldTypeString"}


@dataclass
class LayerReport:
    url: str
    name: str
    fields: list[dict[str, Any]]
    suggestions: dict[str, str]
    date_literal: str
    sample: dict[str, Any] | None = None


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def _type_ok(target: str, field_type: str) -> bool:
    if target in ("price", "beds", "baths", "sqft", "lot_acres", "year_built"):
        return field_type in _NUMERIC_TYPES
    if target == "sale_date":
        return field_type in _DATE_TYPES or field_type in _NUMERIC_TYPES | _TEXT_TYPES
    return field_type in _TEXT_TYPES | _NUMERIC_TYPES


def suggest_fields(fields: list[dict[str, Any]]) -> dict[str, str]:
    """Map our field names onto the layer's, best guess per target.

    Scored on hint specificity and position: an exact "sale_price" beats a
    field that merely contains "price".
    """
    suggestions: dict[str, str] = {}
    for target, hints in _FIELD_HINTS.items():
        best_score = 0.0
        best_name = None
        for field in fields:
            raw_name = field.get("name") or ""
            field_type = field.get("type") or ""
            if not raw_name or not _type_ok(target, field_type):
                continue
            normalized = _normalize(raw_name)
            for index, hint in enumerate(hints):
                if normalized == hint:
                    score = 100 - index
                elif normalized.startswith(hint) or normalized.endswith(hint):
                    score = 60 - index
                elif hint in normalized:
                    score = 30 - index
                else:
                    continue
                if score > best_score:
                    best_score, best_name = score, raw_name
                break
        if best_name:
            suggestions[target] = best_name
    return suggestions


def _date_literal_for(fields: list[dict[str, Any]], date_field: str | None) -> str:
    """Pick the WHERE-clause date syntax matching the field's storage type."""
    if not date_field:
        return "date"
    for field in fields:
        if field.get("name") == date_field:
            field_type = field.get("type") or ""
            if field_type in _DATE_TYPES:
                return "date"
            if field_type in _NUMERIC_TYPES:
                return "epoch_ms"
            return "string"
    return "date"


def inspect_layer(fetcher: Fetcher, layer_url: str, *, sample: bool = True) -> LayerReport:
    url = layer_url.rstrip("/")
    metadata = fetcher.get_json(url, params={"f": "json"})
    if "error" in metadata:
        raise RuntimeError(f"{url}: {metadata['error'].get('message')}")

    fields = metadata.get("fields") or []
    suggestions = suggest_fields(fields)
    report = LayerReport(
        url=url,
        name=metadata.get("name") or url.rsplit("/", 1)[-1],
        fields=[{"name": f.get("name"), "type": f.get("type"), "alias": f.get("alias")} for f in fields],
        suggestions=suggestions,
        date_literal=_date_literal_for(fields, suggestions.get("sale_date")),
    )

    if sample:
        try:
            payload = fetcher.get_json(
                f"{url}/query",
                params={"where": "1=1", "outFields": "*", "resultRecordCount": 1, "f": "json"},
            )
            features = payload.get("features") or []
            if features:
                report.sample = features[0].get("attributes")
        except Exception:
            report.sample = None
    return report


def list_children(fetcher: Fetcher, url: str) -> dict[str, Any]:
    """List services/layers under a server root, folder, or service URL."""
    payload = fetcher.get_json(url.rstrip("/"), params={"f": "json"})
    return {
        "folders": payload.get("folders") or [],
        "services": payload.get("services") or [],
        "layers": payload.get("layers") or [],
        "tables": payload.get("tables") or [],
    }


def looks_like_layer(url: str) -> bool:
    return bool(re.search(r"/(FeatureServer|MapServer)/\d+/?$", url, re.IGNORECASE))


def render_toml(name: str, report: LayerReport, county: str | None = None) -> str:
    """Render a ready-to-paste config block for a discovered layer."""
    suggestions = dict(report.suggestions)
    price = suggestions.pop("price", "SET_ME_price_field")
    sale_date = suggestions.pop("sale_date", "SET_ME_date_field")

    lines = [
        f"[sources.{name}]",
        'type = "arcgis"',
        "enabled = true",
        "rank = 90",
        f'service_url = "{report.url}"',
        f'price_field = "{price}"',
        f'date_field = "{sale_date}"',
        f'date_literal = "{report.date_literal}"',
    ]
    if county:
        lines.append(f'county = "{county}"')
    lines.append("return_geometry = false")
    lines.append("")
    lines.append(f"[sources.{name}.field_map]")
    for target in ("address", "municipality", "zip_code", "parcel_id", "buyer", "seller",
                   "sqft", "lot_acres", "year_built", "property_type"):
        if target in suggestions:
            lines.append(f'{target} = "{suggestions[target]}"')
    if "address" not in suggestions:
        lines.append('address = "SET_ME_address_field"')
    return "\n".join(lines)
