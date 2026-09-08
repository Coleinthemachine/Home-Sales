"""Source plugins. Importing this package registers every built-in type."""

from .base import (  # noqa: F401
    ConfigurationError,
    Source,
    build_source,
    get_source_class,
    iter_sources,
    register,
    registered_types,
)
from . import arcgis, html_table, json_api, news_rss  # noqa: F401

__all__ = [
    "ConfigurationError",
    "Source",
    "build_source",
    "get_source_class",
    "iter_sources",
    "register",
    "registered_types",
]
