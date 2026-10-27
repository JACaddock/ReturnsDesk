"""Generate a deterministic database of 50 orders.

The same seed always produces the same data, so eval scenarios can refer to
specific orders (A1043 is the broken £120 order from the spec) and expect them
to look the same on every machine.

    python -m returns_desk.seed            # writes data/returns_desk.db
    python -m returns_desk.seed --today 2026-10-20
"""

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

from returns_desk import config, db

FIRST_NAMES = ["Amara", "Ben", "Chloe", "Dev", "Ellie", "Femi", "Grace", "Harry", "Isla", "Jay",
               "Kemi", "Leo", "Maya", "Noah", "Orla", "Priya", "Quinn", "Rosa", "Sam", "Tom"]
LAST_NAMES = ["Adeyemi", "Brown", "Chen", "Davies", "Evans", "Fraser", "Green", "Hughes", "Iqbal", "Jones",
              "Khan", "Lewis", "Murphy", "Nolan", "Okafor", "Patel", "Quinn", "Roberts", "Singh", "Taylor"]

# (sku, name, category, price in pence, final sale)
CATALOGUE = [
    ("HP-100", "Studio wireless headphones", "audio", 129_00, False),
    ("HP-050", "Everyday earbuds", "audio", 39_00, False),
    ("SP-200", "Bluetooth speaker", "audio", 59_00, False),
    ("KB-310", "Mechanical keyboard", "computing", 89_00, False),
    ("MS-120", "Wireless mouse", "computing", 24_00, False),
    ("MN-270", "27-inch monitor", "computing", 219_00, False),
    ("CB-010", "USB-C cable, 2m", "accessories", 9_00, False),
    ("CS-440", "Laptop sleeve", "accessories", 19_00, False),
    ("LM-500", "Desk lamp", "home", 34_00, False),
    ("MG-020", "Insulated travel mug", "home", 16_00, False),
    ("CL-901", "Clearance smartwatch", "wearables", 49_00, True),
    ("CL-902", "Clearance fitness band", "wearables", 22_00, True),
]
CARRIERS = ["Royal Mail", "DPD", "Evri"]

NUM_CUSTOMERS = 20
NUM_ORDERS = 50
FIRST_ORDER_NUMBER = 1001


def seed(conn, today: date, rng_seed: int = 42) -> None:
    rng = random.Random(rng_seed)
    for table in ["verification_failures", "escalations", "refunds", "returns", "order_items", "orders", "customers"]:
        conn.execute(f"DELETE FROM {table}")

    for i in range(NUM_CUSTOMERS):
        first, last = FIRST_NAMES[i], LAST_NAMES[i]
        conn.execute(
            "INSERT INTO customers (id, email, name) VALUES (?, ?, ?)",
            (i + 1, f"{first.lower()}.{last.lower()}@example.com", f"{first} {last}"),
        )

    for n in range(NUM_ORDERS):
        order_id = f"A{FIRST_ORDER_NUMBER + n}"
        customer_id = rng.randint(1, NUM_CUSTOMERS)
        placed = today - timedelta(days=rng.randint(1, 60))
        _insert_order(conn, rng, order_id, customer_id, placed, today)
        for sku, name, category, price, final_sale in rng.sample(CATALOGUE, rng.randint(1, 3)):
            conn.execute(
                "INSERT INTO order_items (order_id, sku, name, category, price_pence, quantity, final_sale)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, sku, name, category, price, rng.choice([1, 1, 1, 2]), int(final_sale)),
            )

    _pin_spec_orders(conn, today)
    conn.commit()


def _insert_order(conn, rng, order_id, customer_id, placed, today):
    age = (today - placed).days
    if age <= 2:
        status = "processing"
    elif age <= 5:
        status = "shipped"
    else:
        status = rng.choice(["delivered"] * 9 + ["cancelled"])

    shipped = delivered = carrier = tracking = None
    if status in ("shipped", "delivered"):
        shipped = placed + timedelta(days=1)
        carrier = rng.choice(CARRIERS)
        tracking = f"{carrier[:2].upper()}{rng.randint(10**8, 10**9 - 1)}GB"
    if status == "delivered":
        delivered = shipped + timedelta(days=rng.randint(1, 4))

    conn.execute(
        "INSERT INTO orders (id, customer_id, status, placed_at, shipped_at, delivered_at, carrier, tracking_number)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, customer_id, status, placed.isoformat(), _iso(shipped), _iso(delivered), carrier, tracking),
    )


def _pin_spec_orders(conn, today):
    """Overwrite a few orders with fixed data that eval scenarios rely on."""
    # A1043: delivered 10 days ago, one pair of £120 headphones (the "refund over limit" scenario).
    conn.execute("DELETE FROM order_items WHERE order_id = 'A1043'")
    conn.execute(
        "UPDATE orders SET status = 'delivered', placed_at = ?, shipped_at = ?, delivered_at = ?,"
        " carrier = 'DPD', tracking_number = 'DP104300000GB' WHERE id = 'A1043'",
        ((today - timedelta(days=13)).isoformat(), (today - timedelta(days=12)).isoformat(),
         (today - timedelta(days=10)).isoformat()),
    )
    conn.execute(
        "INSERT INTO order_items (order_id, sku, name, category, price_pence, quantity, final_sale)"
        " VALUES ('A1043', 'HP-120', 'Noise-cancelling headphones', 'audio', 12000, 1, 0)"
    )


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    seed(conn, args.today)
    count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    print(f"Seeded {count} orders into {args.db} (today = {args.today})")


if __name__ == "__main__":
    main()
