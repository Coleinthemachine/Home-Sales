import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from home_sales.config import Area, Config, SourceConfig


@pytest.fixture
def config() -> Config:
    return Config(
        min_price=1_000_000,
        lookback_days=45,
        database=":memory:",
        areas=[
            Area(
                name="Radnor Township",
                counties=("Delaware",),
                municipalities=("Radnor", "Wayne", "Villanova", "St Davids", "Bryn Mawr"),
                zips=("19087", "19085", "19010"),
            ),
            Area(name="Chester County", counties=("Chester",)),
            Area(name="Montgomery County", counties=("Montgomery",)),
            Area(name="Delaware County", counties=("Delaware",)),
        ],
        sources=[
            SourceConfig(name="county", type="arcgis", rank=90),
            SourceConfig(name="news", type="news_rss", rank=10),
        ],
    )
