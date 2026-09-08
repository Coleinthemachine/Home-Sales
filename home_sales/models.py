"""The Sale record and its merge semantics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from . import normalize


@dataclass(slots=True)
class Sale:
    """One residential transaction, as reported by one source.

    Field confidence varies by source: county records are authoritative on
    price, date and parties but thin on bed/bath detail; listing APIs are the
    reverse. `merge` encodes that.
    """

    address: str
    price: int
    sale_date: date
    source: str
    county: str | None = None
    municipality: str | None = None
    state: str = "PA"
    zip_code: str | None = None
    unit: str | None = None
    parcel_id: str | None = None
    buyer: str | None = None
    seller: str | None = None
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    lot_acres: float | None = None
    year_built: int | None = None
    property_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_url: str | None = None
    area: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        base, unit = normalize.split_unit(self.address)
        self.address = normalize.title_case_address(base)
        if unit and not self.unit:
            self.unit = unit
        self.zip_code = normalize.normalize_zip(self.zip_code)

    @property
    def property_key(self) -> str:
        return normalize.property_key(self.display_address, self.zip_code, self.municipality)

    @property
    def display_address(self) -> str:
        if self.unit:
            return f"{self.address} #{self.unit}"
        return self.address

    @property
    def normalized_municipality(self) -> str:
        return normalize.normalize_municipality(self.municipality or "")

    @property
    def normalized_county(self) -> str:
        return normalize.normalize_county(self.county or "")

    def merge(self, other: "Sale", source_ranks: dict[str, int]) -> "Sale":
        """Combine two reports of the same transaction.

        Scalar conflicts resolve toward the higher-ranked source; missing values
        are filled from whichever record has them. `raw` keeps both payloads so
        nothing observed is lost.
        """
        self_rank = source_ranks.get(self.source, 0)
        other_rank = source_ranks.get(other.source, 0)
        primary, secondary = (self, other) if self_rank >= other_rank else (other, self)

        merged = replace(primary)
        for slot in Sale.__slots__:
            if slot in ("raw", "first_seen", "last_seen", "source", "source_url"):
                continue
            if getattr(merged, slot, None) in (None, "", 0):
                fallback = getattr(secondary, slot, None)
                if fallback not in (None, "", 0):
                    setattr(merged, slot, fallback)

        merged.raw = {**secondary.raw, **primary.raw}
        merged.source = primary.source
        merged.source_url = primary.source_url or secondary.source_url
        merged.first_seen = min(self.first_seen, other.first_seen)
        merged.last_seen = max(self.last_seen, other.last_seen)
        return merged

    def to_row(self) -> dict[str, Any]:
        return {
            "property_key": self.property_key,
            "address": self.address,
            "unit": self.unit,
            "municipality": self.municipality,
            "county": self.county,
            "state": self.state,
            "zip_code": self.zip_code,
            "price": self.price,
            "sale_date": self.sale_date.isoformat(),
            "parcel_id": self.parcel_id,
            "buyer": self.buyer,
            "seller": self.seller,
            "beds": self.beds,
            "baths": self.baths,
            "sqft": self.sqft,
            "lot_acres": self.lot_acres,
            "year_built": self.year_built,
            "property_type": self.property_type,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "source": self.source,
            "source_url": self.source_url,
            "area": self.area,
            "raw": json.dumps(self.raw, default=str),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Sale":
        def as_dt(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return datetime.now(timezone.utc)

        sale = cls(
            address=row["address"],
            price=int(row["price"]),
            sale_date=normalize.parse_date(row["sale_date"]) or date.today(),
            source=row["source"],
            county=row.get("county"),
            municipality=row.get("municipality"),
            state=row.get("state") or "PA",
            zip_code=row.get("zip_code"),
            unit=row.get("unit"),
            parcel_id=row.get("parcel_id"),
            buyer=row.get("buyer"),
            seller=row.get("seller"),
            beds=row.get("beds"),
            baths=row.get("baths"),
            sqft=row.get("sqft"),
            lot_acres=row.get("lot_acres"),
            year_built=row.get("year_built"),
            property_type=row.get("property_type"),
            latitude=row.get("latitude"),
            longitude=row.get("longitude"),
            source_url=row.get("source_url"),
            area=row.get("area"),
            raw=json.loads(row["raw"]) if row.get("raw") else {},
        )
        sale.first_seen = as_dt(row.get("first_seen"))
        sale.last_seen = as_dt(row.get("last_seen"))
        return sale


def build_sale(
    *,
    address: str,
    price: object,
    sale_date: object,
    source: str,
    **kwargs: Any,
) -> Sale | None:
    """Construct a Sale from raw source values, or None if unusable.

    A record without a parseable address, price and date can't be deduped or
    filtered, so it is dropped rather than stored half-formed.
    """
    parsed_price = normalize.parse_price(price)
    parsed_date = normalize.parse_date(sale_date)
    clean_address = (address or "").strip()
    if not clean_address or parsed_price is None or parsed_date is None:
        return None
    if not any(char.isdigit() for char in clean_address):
        return None

    allowed = {slot for slot in Sale.__slots__}
    filtered = {key: value for key, value in kwargs.items() if key in allowed}
    return Sale(
        address=clean_address,
        price=parsed_price,
        sale_date=parsed_date,
        source=source,
        **filtered,
    )
