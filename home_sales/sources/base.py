"""Source interface and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any, Iterable, Iterator, Type

from ..config import Config, SourceConfig
from ..http import Fetcher
from ..models import Sale

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Type["Source"]] = {}


def register(type_name: str):
    def decorator(cls: Type["Source"]) -> Type["Source"]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def get_source_class(type_name: str) -> Type["Source"]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"Unknown source type {type_name!r}. Known types: {known}") from None


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


class ConfigurationError(RuntimeError):
    """The source cannot run as configured -- a missing key, URL or field map."""


class Source(ABC):
    type_name: str = "base"

    def __init__(self, spec: SourceConfig, config: Config, fetcher: Fetcher) -> None:
        self.spec = spec
        self.config = config
        self.fetcher = fetcher
        self.options: dict[str, Any] = spec.options

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def since(self) -> date:
        return date.today() - timedelta(days=self.config.lookback_days)

    def option(self, key: str, default: Any = None, *, required: bool = False) -> Any:
        value = self.options.get(key, default)
        if required and value in (None, "", []):
            raise ConfigurationError(
                f"Source {self.name!r} is missing required option {key!r}. "
                f"Add it under [sources.{self.name}] in config.toml."
            )
        return value

    @abstractmethod
    def fetch(self) -> Iterable[Sale]:
        """Yield sales. Filtering by price/area happens downstream."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


def build_source(spec: SourceConfig, config: Config, fetcher: Fetcher) -> Source:
    return get_source_class(spec.type)(spec, config, fetcher)


def iter_sources(config: Config, fetcher: Fetcher) -> Iterator[Source]:
    for spec in config.enabled_sources():
        yield build_source(spec, config, fetcher)
