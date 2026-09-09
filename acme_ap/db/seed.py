"""Seed the mock inventory.

The four rows the case README specifies are the contract with the sample
invoices and are reproduced exactly. Unit prices and aliases are additions: the
prices let validation catch a vendor quietly repricing an item, and the aliases
absorb the spelling drift real documents contain.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from acme_ap.db.migrations import connect
from acme_ap.logging import get_logger

logger = get_logger(__name__)

# (item, stock, unit_price, category) -- stock values are the README's.
INVENTORY: list[tuple[str, int, float | None, str]] = [
    ("WidgetA", 15, 250.00, "widgets"),
    ("WidgetB", 10, 500.00, "widgets"),
    ("GadgetX", 5, 750.00, "gadgets"),
    ("FakeItem", 0, None, "unknown"),
]

# Spelling variants observed across the sample documents, plus the obvious
# case/spacing permutations. Resolution is data, not prompt instructions.
ALIASES: list[tuple[str, str]] = [
    ("widgeta", "WidgetA"),
    ("widget a", "WidgetA"),
    ("widget-a", "WidgetA"),
    ("widget_a", "WidgetA"),
    ("widgetb", "WidgetB"),
    ("widget b", "WidgetB"),
    ("widget-b", "WidgetB"),
    ("widget_b", "WidgetB"),
    ("gadgetx", "GadgetX"),
    ("gadget x", "GadgetX"),
    ("gadget-x", "GadgetX"),
    ("gadget_x", "GadgetX"),
    ("fakeitem", "FakeItem"),
    ("fake item", "FakeItem"),
]


def seed(path: Path | None = None, *, reset: bool = False) -> None:
    """Populate reference data. Idempotent unless ``reset`` is set."""
    conn = connect(path)
    try:
        if reset:
            conn.execute("DELETE FROM item_aliases")
            conn.execute("DELETE FROM inventory")
        conn.executemany(
            "INSERT OR IGNORE INTO inventory (item, stock, unit_price, category)"
            " VALUES (?, ?, ?, ?)",
            INVENTORY,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO item_aliases (alias, canonical_item) VALUES (?, ?)",
            ALIASES,
        )
        conn.commit()
        logger.info("inventory seeded", extra={"items": len(INVENTORY), "aliases": len(ALIASES)})
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    from acme_ap.db.migrations import apply
    from acme_ap.logging import configure_logging

    configure_logging("console")
    apply()
    seed()
    conn = connect()
    for row in conn.execute("SELECT item, stock FROM inventory ORDER BY item"):
        print(f"  {row['item']:<10} {row['stock']}")
    conn.close()
