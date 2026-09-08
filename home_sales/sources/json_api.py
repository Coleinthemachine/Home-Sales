"""Config-driven JSON API source for licensed data providers.

Deliberately generic. RentCast, ATTOM and similar vendors all expose "sold
properties in an area" as paginated JSON; they differ only in URL, auth header,
parameter names and where the records sit in the response. Encoding that as
configuration means adding a provider (or surviving one's schema change) is a
config edit rather than a new Python class.

See config.example.toml for worked RentCast and ATTOM blocks.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator

from ..models import Sale, build_sale
from .base import ConfigurationError, Source, register

log = logging.getLogger(__name__)

_MAX_PAGES_DEFAULT = 20


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts/lists.

    "data.results" walks two dicts; "0.price" indexes a list. Returns None if
    any step is missing, so a provider omitting an optional field is not fatal.
    """
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return None
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


@register("json_api")
class JsonApiSource(Source):
    def fetch(self) -> Iterable[Sale]:
        url = str(self.option("url", required=True))
        field_map: dict[str, str] = dict(self.option("field_map", {}) or {})
        for required_key in ("address", "price", "sale_date"):
            if required_key not in field_map:
                raise ConfigurationError(
                    f"Source {self.name!r} needs field_map.{required_key}."
                )

        headers = self._resolve_headers()
        records_path = str(self.option("records_path", ""))

        count = 0
        for record in self._iter_records(url, headers, records_path):
            sale = self._to_sale(record, field_map, url)
            if sale is not None:
                count += 1
                yield sale
        log.info("%s: produced %d sales", self.name, count)

    def _resolve_headers(self) -> dict[str, str]:
        headers = {str(k): str(v) for k, v in (self.option("headers", {}) or {}).items()}
        for key, value in headers.items():
            if value.startswith("$") or not value.strip():
                raise ConfigurationError(
                    f"Source {self.name!r} header {key!r} is unset -- it still reads {value!r}. "
                    "Export the environment variable it references, or disable this source."
                )
        return headers

    def _substitute(self, value: Any) -> Any:
        """Fill run-time placeholders in configured query parameters."""
        if not isinstance(value, str):
            return value
        return (
            value.replace("{since}", self.since.isoformat())
            .replace("{min_price}", str(self.config.min_price))
            .replace("{lookback_days}", str(self.config.lookback_days))
        )

    def _iter_records(
        self, url: str, headers: dict[str, str], records_path: str
    ) -> Iterator[dict[str, Any]]:
        base_params = {
            str(k): self._substitute(v) for k, v in (self.option("params", {}) or {}).items()
        }
        mode = str(self.option("paginate", "none")).lower()
        page_param = str(self.option("page_param", "offset"))
        size_param = self.option("size_param")
        page_size = int(self.option("page_size", 100))
        max_pages = int(self.option("max_pages", _MAX_PAGES_DEFAULT))

        cursor = int(self.option("page_start", 0))
        for _ in range(max_pages):
            params = dict(base_params)
            if mode != "none":
                params[page_param] = cursor
                if size_param:
                    params[str(size_param)] = page_size

            payload = self.fetcher.get_json(url, params=params, headers=headers)
            records = dig(payload, records_path) if records_path else payload
            if isinstance(records, dict):
                records = [records]
            if not records:
                return
            if not isinstance(records, list):
                raise ConfigurationError(
                    f"{self.name}: records_path {records_path!r} did not resolve to a list "
                    f"(got {type(records).__name__}). Check the provider's response shape."
                )

            for record in records:
                if isinstance(record, dict):
                    yield record

            if mode == "none" or len(records) < page_size:
                return
            # "offset" counts records consumed; "page" counts pages.
            cursor += page_size if mode == "offset" else 1

    def _to_sale(
        self, record: dict[str, Any], field_map: dict[str, str], url: str
    ) -> Sale | None:
        values: dict[str, Any] = {}
        for target, path in field_map.items():
            found = dig(record, str(path))
            if found not in (None, ""):
                values[target] = found

        address = values.pop("address", None)
        price = values.pop("price", None)
        sale_date = values.pop("sale_date", None)
        if address is None:
            return None

        # Providers often return address as an object or split lines.
        if isinstance(address, dict):
            address = address.get("line1") or address.get("street") or address.get("full")
        if not address:
            return None

        values.setdefault("county", self.option("county"))
        values.setdefault("municipality", self.option("municipality"))
        values = {key: value for key, value in values.items() if value is not None}

        return build_sale(
            address=str(address),
            price=price,
            sale_date=sale_date,
            source=self.name,
            source_url=values.pop("source_url", None) or url,
            raw=record,
            **values,
        )
