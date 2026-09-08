"""SQLite persistence with merge-on-write dedupe."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import Sale

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    property_key  TEXT    NOT NULL,
    address       TEXT    NOT NULL,
    unit          TEXT,
    municipality  TEXT,
    county        TEXT,
    state         TEXT,
    zip_code      TEXT,
    price         INTEGER NOT NULL,
    sale_date     TEXT    NOT NULL,
    parcel_id     TEXT,
    buyer         TEXT,
    seller        TEXT,
    beds          REAL,
    baths         REAL,
    sqft          INTEGER,
    lot_acres     REAL,
    year_built    INTEGER,
    property_type TEXT,
    latitude      REAL,
    longitude     REAL,
    source        TEXT    NOT NULL,
    source_url    TEXT,
    area          TEXT,
    raw           TEXT,
    first_seen    TEXT,
    last_seen     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sales_property ON sales(property_key, sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_date     ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_price    ON sales(price);
CREATE INDEX IF NOT EXISTS idx_sales_area     ON sales(area);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    new_sales     INTEGER DEFAULT 0,
    updated_sales INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    fetched     INTEGER DEFAULT 0,
    kept        INTEGER DEFAULT 0,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs_run ON source_runs(run_id);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def upsert(self, sale: Sale, source_ranks: dict[str, int], window_days: int = 14) -> bool:
        """Insert a sale or merge it into a near-duplicate. Returns True if new.

        Sources report the same transaction under slightly different dates
        (deed recording vs. settlement), so a same-property sale within
        `window_days` is treated as the same event.
        """
        existing_row = self._find_near_duplicate(sale, window_days)
        with self._tx() as conn:
            if existing_row is None:
                row = sale.to_row()
                columns = ", ".join(row)
                placeholders = ", ".join(f":{key}" for key in row)
                conn.execute(f"INSERT INTO sales ({columns}) VALUES ({placeholders})", row)
                return True

            existing = Sale.from_row(dict(existing_row))
            merged = existing.merge(sale, source_ranks)
            merged.last_seen = datetime.now(timezone.utc)
            row = merged.to_row()
            assignments = ", ".join(f"{key} = :{key}" for key in row)
            conn.execute(
                f"UPDATE sales SET {assignments} WHERE id = :id",
                {**row, "id": existing_row["id"]},
            )
            return False

    def _find_near_duplicate(self, sale: Sale, window_days: int) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM sales
            WHERE property_key = ?
              AND ABS(julianday(sale_date) - julianday(?)) <= ?
            ORDER BY ABS(julianday(sale_date) - julianday(?))
            LIMIT 1
            """,
            (
                sale.property_key,
                sale.sale_date.isoformat(),
                window_days,
                sale.sale_date.isoformat(),
            ),
        ).fetchone()

    def query_sales(
        self,
        *,
        min_price: int | None = None,
        since: date | None = None,
        area: str | None = None,
        county: str | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Sale]:
        clauses: list[str] = []
        params: list[Any] = []
        if min_price is not None:
            clauses.append("price >= ?")
            params.append(min_price)
        if since is not None:
            clauses.append("sale_date >= ?")
            params.append(since.isoformat())
        if area:
            clauses.append("area = ?")
            params.append(area)
        if county:
            clauses.append("county = ?")
            params.append(county)
        if search:
            clauses.append("(address LIKE ? OR municipality LIKE ? OR buyer LIKE ? OR seller LIKE ?)")
            params.extend([f"%{search}%"] * 4)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM sales {where} ORDER BY sale_date DESC, price DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [Sale.from_row(dict(row)) for row in rows]

    def count_sales(self, *, min_price: int | None = None, since: date | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if min_price is not None:
            clauses.append("price >= ?")
            params.append(min_price)
        if since is not None:
            clauses.append("sale_date >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM sales {where}", params).fetchone()
        return int(row["n"])

    def distinct_values(self, column: str) -> list[str]:
        if column not in {"area", "county", "municipality", "source"}:
            raise ValueError(f"Refusing to select unexpected column: {column}")
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} AS value FROM sales "
            f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY value"
        ).fetchall()
        return [row["value"] for row in rows]

    def start_run(self, run_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at) VALUES (?, ?)",
                (run_id, datetime.now(timezone.utc).isoformat()),
            )

    def finish_run(self, run_id: str, new_sales: int, updated_sales: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, new_sales = ?, updated_sales = ? WHERE run_id = ?",
                (datetime.now(timezone.utc).isoformat(), new_sales, updated_sales, run_id),
            )

    def record_source_run(
        self,
        run_id: str,
        source: str,
        status: str,
        started_at: datetime,
        fetched: int,
        kept: int,
        message: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO source_runs
                    (run_id, source, status, started_at, finished_at, fetched, kept, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    status,
                    started_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    fetched,
                    kept,
                    message,
                ),
            )

    def latest_source_status(self) -> list[dict[str, Any]]:
        """Most recent outcome per source, so a silently broken feed is visible."""
        rows = self._conn.execute(
            """
            SELECT sr.* FROM source_runs sr
            JOIN (
                SELECT source, MAX(id) AS max_id FROM source_runs GROUP BY source
            ) latest ON sr.id = latest.max_id
            ORDER BY sr.source
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def last_run(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def all_sales_for_export(self, min_price: int | None = None) -> Sequence[Sale]:
        return self.query_sales(min_price=min_price, limit=1_000_000)
