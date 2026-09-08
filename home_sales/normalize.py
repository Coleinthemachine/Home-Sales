"""Address, price and date normalization shared by every source.

Sources disagree on formatting far more than on substance: one county writes
"123 NORTH WAYNE AVENUE APT 4", an API writes "123 N Wayne Ave #4". Everything
here exists to collapse those into one comparable form so dedupe works.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

_DIRECTIONS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}

_STREET_TYPES = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "AV": "AVE",
    "ROAD": "RD",
    "DRIVE": "DR",
    "LANE": "LN",
    "COURT": "CT",
    "CIRCLE": "CIR",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "TERRACE": "TER",
    "PARKWAY": "PKWY",
    "HIGHWAY": "HWY",
    "TRAIL": "TRL",
    "SQUARE": "SQ",
    "TURNPIKE": "TPKE",
    "CRESCENT": "CRES",
    "COMMONS": "CMNS",
    "EXTENSION": "EXT",
    "HEIGHTS": "HTS",
    "LOOP": "LOOP",
    "RUN": "RUN",
    "WAY": "WAY",
}

# Ordinals appear as "1ST"/"FIRST" interchangeably in street names.
_ORDINAL_WORDS = {
    "FIRST": "1ST",
    "SECOND": "2ND",
    "THIRD": "3RD",
    "FOURTH": "4TH",
    "FIFTH": "5TH",
    "SIXTH": "6TH",
    "SEVENTH": "7TH",
    "EIGHTH": "8TH",
    "NINTH": "9TH",
    "TENTH": "10TH",
}

_UNIT_MARKERS = ("APT", "APARTMENT", "UNIT", "STE", "SUITE", "#", "LOT", "FL", "FLOOR")

_UNIT_RE = re.compile(
    r"\b(?:APT|APARTMENT|UNIT|STE|SUITE|LOT|FL|FLOOR)\.?\s*([A-Z0-9][A-Z0-9\-]*)\b|#\s*([A-Z0-9][A-Z0-9\-]*)\b"
)

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(K|M|MM|MILLION|THOUSAND)?", re.IGNORECASE)

_MULTIPLIERS = {
    None: 1,
    "": 1,
    "K": 1_000,
    "THOUSAND": 1_000,
    "M": 1_000_000,
    "MM": 1_000_000,
    "MILLION": 1_000_000,
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def split_unit(address: str) -> tuple[str, str | None]:
    """Split a trailing unit designator off an address line."""
    text = (address or "").upper().strip()
    match = _UNIT_RE.search(text)
    if not match:
        return text, None
    unit = match.group(1) or match.group(2)
    remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
    return remainder, unit


def normalize_address(address: str) -> str:
    """Reduce a street address to a stable comparison key.

    Not USPS-correct standardization -- just consistent enough that two sources
    describing the same house produce the same string.
    """
    text, _ = split_unit(address or "")
    text = text.replace("&", " AND ")
    text = re.sub(r"[.,]", " ", text)
    text = re.sub(r"[^A-Z0-9\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    tokens = []
    for token in text.split(" "):
        token = token.strip("-")
        if not token:
            continue
        token = _ORDINAL_WORDS.get(token, token)
        token = _DIRECTIONS.get(token, token)
        token = _STREET_TYPES.get(token, token)
        tokens.append(token)

    # Trailing directional suffixes ("MAIN ST W") are noise for matching.
    while len(tokens) > 2 and tokens[-1] in _DIRECTIONS.values():
        tokens.pop()
    return " ".join(tokens)


def normalize_municipality(name: str) -> str:
    """Normalize a municipality name for comparison.

    County records say "RADNOR TWP", listings say "Radnor Township", news says
    "Radnor". All three collapse to "RADNOR".
    """
    text = (name or "").upper().strip()
    text = re.sub(r"[.,]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "City of Chester" and "Chester City" are the same place.
    text = re.sub(r"^(TOWNSHIP|TWP|BOROUGH|BORO|CITY|TOWN|VILLAGE)\s+OF\s+", "", text)
    text = re.sub(r"\b(TOWNSHIP|TWP|BOROUGH|BORO|CITY|TOWN|VILLAGE)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_county(name: str) -> str:
    text = (name or "").upper().strip()
    text = re.sub(r"\bCOUNTY\b", " ", text)
    text = re.sub(r"[^A-Z\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_zip(value: object) -> str | None:
    """Return a 5-digit ZIP, or None when the input isn't one."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{5})(?:-\d{4})?$", text)
    if match:
        return match.group(1)
    # Some feeds emit ZIPs as integers, dropping a leading zero (PA has none,
    # but the padding keeps this correct if reused elsewhere).
    if text.isdigit() and len(text) in (4, 5):
        return text.zfill(5)
    return None


def parse_price(value: object) -> int | None:
    """Parse a price into whole dollars.

    Handles "$1,250,000", "1.25M", "1250000.00" and bare numbers.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = int(round(float(value)))
        return price if price > 0 else None

    text = str(value).strip()
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    number = match.group(1).replace(",", "")
    suffix = (match.group(2) or "").upper()
    try:
        amount = float(number) * _MULTIPLIERS.get(suffix, 1)
    except ValueError:
        return None
    price = int(round(amount))
    return price if price > 0 else None


def parse_date(value: object) -> date | None:
    """Parse a date from the many shapes sources use, including epoch millis."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    # ArcGIS returns epoch milliseconds for date fields.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number <= 0:
            return None
        seconds = number / 1000.0 if number > 1e11 else number
        try:
            return datetime.utcfromtimestamp(seconds).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 10:
        return parse_date(int(text))

    cleaned = text.replace("Z", "").split(".")[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        return None


def title_case_address(address: str) -> str:
    """Human-friendly casing for display, preserving directionals and ordinals."""
    text = re.sub(r"\s+", " ", (address or "").strip())
    if not text:
        return ""
    out = []
    for token in text.split(" "):
        upper = token.upper()
        if upper in _DIRECTIONS.values() or upper in {"NE", "NW", "SE", "SW"}:
            out.append(upper)
        elif re.match(r"^\d+(ST|ND|RD|TH)$", upper):
            out.append(upper)
        elif upper.isdigit():
            out.append(upper)
        else:
            out.append(token.capitalize())
    return " ".join(out)


def property_key(address: str, zip_code: str | None, municipality: str | None) -> str:
    """Stable identifier for a physical property.

    ZIP is preferred as the locality discriminator; municipality is the fallback
    for sources that omit it. Unit is included so condos don't collapse together.
    """
    _, unit = split_unit(address or "")
    locality = normalize_zip(zip_code) or normalize_municipality(municipality or "")
    parts = [normalize_address(address or ""), unit or "", locality]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]
