"""SQLite schema and connection helper.

Money is stored in pence as integers so nothing is ever off by a rounding error.
The seed fills customers, orders and order_items; the tools write everything else.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,          -- e.g. A1043
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    status          TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'cancelled')),
    placed_at       TEXT NOT NULL,             -- ISO date
    shipped_at      TEXT,
    delivered_at    TEXT,
    carrier         TEXT,
    tracking_number TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    sku         TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price_pence INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    final_sale  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS returns (
    id          INTEGER PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(id),
    item_id     INTEGER NOT NULL REFERENCES order_items(id),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id           INTEGER PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    amount_pence INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY,
    order_id    TEXT REFERENCES orders(id),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- No foreign key: guesses at order numbers that don't exist are recorded too.
CREATE TABLE IF NOT EXISTS verification_failures (
    id          INTEGER PRIMARY KEY,
    order_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)  # every statement is IF NOT EXISTS, so older databases gain new tables
    return conn
