"""Configuration loading.

Endpoints, field mappings and target areas live in TOML rather than code so a
county changing a layer name is an edit, not a patch.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import normalize

DEFAULT_CONFIG_PATH = Path("config.toml")


@dataclass(frozen=True)
class Area:
    """A geography we care about.

    A record matches if it satisfies *any* of the configured signals. Counties
    are the coarse filter; municipalities and ZIPs let a sub-county area like
    Radnor Township be tracked and labeled on its own.
    """

    name: str
    counties: tuple[str, ...] = ()
    municipalities: tuple[str, ...] = ()
    zips: tuple[str, ...] = ()

    @property
    def normalized_counties(self) -> set[str]:
        return {normalize.normalize_county(c) for c in self.counties if c}

    @property
    def normalized_municipalities(self) -> set[str]:
        return {normalize.normalize_municipality(m) for m in self.municipalities if m}

    def matches(self, county: str, municipality: str, zip_code: str | None) -> bool:
        if zip_code and zip_code in self.zips:
            return True
        if municipality and municipality in self.normalized_municipalities:
            return True
        if county and county in self.normalized_counties:
            # A county-wide area claims everything in it; an area that also
            # names municipalities is a sub-county area and must match those.
            return not self.municipalities and not self.zips
        return False


@dataclass
class SourceConfig:
    name: str
    type: str
    enabled: bool = True
    rank: int = 0
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    min_price: int = 1_000_000
    lookback_days: int = 45
    database: str = "data/home_sales.db"
    user_agent: str = (
        "home-sales-bot/0.1 (public-records research; contact: set contact_email in config.toml)"
    )
    contact_email: str | None = None
    request_delay_seconds: float = 1.5
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    respect_robots: bool = True
    dedupe_window_days: int = 14
    areas: list[Area] = field(default_factory=list)
    sources: list[SourceConfig] = field(default_factory=list)

    @property
    def source_ranks(self) -> dict[str, int]:
        return {source.name: source.rank for source in self.sources}

    def enabled_sources(self) -> list[SourceConfig]:
        return [source for source in self.sources if source.enabled]

    def match_area(self, county: str, municipality: str, zip_code: str | None) -> Area | None:
        """Return the most specific matching area, or None.

        Sub-county areas are checked first so a Radnor sale is labeled "Radnor
        Township" rather than the broader "Delaware County".
        """
        specific = [a for a in self.areas if a.municipalities or a.zips]
        broad = [a for a in self.areas if not (a.municipalities or a.zips)]
        for area in specific + broad:
            if area.matches(county, municipality, zip_code):
                return area
        return None


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} references so API keys stay out of the config file."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"No config file at {config_path}. Run from the repository root, or pass "
            "--config /path/to/config.toml."
        )

    with config_path.open("rb") as handle:
        data = _expand_env(tomllib.load(handle))

    filters = data.get("filters", {})
    fetching = data.get("fetching", {})
    storage = data.get("storage", {})

    areas = [
        Area(
            name=entry["name"],
            counties=tuple(entry.get("counties", [])),
            municipalities=tuple(entry.get("municipalities", [])),
            zips=tuple(str(z) for z in entry.get("zips", [])),
        )
        for entry in data.get("areas", [])
    ]

    sources = []
    for name, entry in (data.get("sources") or {}).items():
        entry = dict(entry)
        sources.append(
            SourceConfig(
                name=name,
                type=entry.pop("type"),
                enabled=bool(entry.pop("enabled", True)),
                rank=int(entry.pop("rank", 0)),
                options=entry,
            )
        )

    return Config(
        min_price=int(filters.get("min_price", 1_000_000)),
        lookback_days=int(filters.get("lookback_days", 45)),
        dedupe_window_days=int(filters.get("dedupe_window_days", 14)),
        database=storage.get("database", "data/home_sales.db"),
        user_agent=fetching.get("user_agent", Config.user_agent),
        contact_email=fetching.get("contact_email"),
        request_delay_seconds=float(fetching.get("request_delay_seconds", 1.5)),
        request_timeout_seconds=float(fetching.get("request_timeout_seconds", 30.0)),
        max_retries=int(fetching.get("max_retries", 3)),
        respect_robots=bool(fetching.get("respect_robots", True)),
        areas=areas,
        sources=sources,
    )
