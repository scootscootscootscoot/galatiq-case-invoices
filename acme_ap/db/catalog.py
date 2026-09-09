"""Item-name resolution against the inventory catalog.

Deliberately *not* fuzzy. Item codes are identifiers, not prose: ``WidgetC``
differing from ``WidgetB`` by one character means a different product, not a
misspelling. Generic similarity matching resolves ``WidgetC`` to ``WidgetB`` at
0.86 confidence and pays for goods that were never ordered.

Priority ladder: exact match, then curated alias, then normalised form, then a
repair pass for known OCR character confusions -- each of which must land exactly
on a catalog entry. Anything else is ``UNKNOWN_ITEM`` upstream.
"""

from __future__ import annotations

import re
import sqlite3

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase and strip punctuation/whitespace for comparison purposes."""
    return _NON_ALNUM.sub("", text.lower().strip())


# Characters optical character recognition routinely swaps. Substitutions are
# applied only to produce candidates that must then match the catalog exactly,
# so this can widen recall without ever inventing a match.
_OCR_CONFUSIONS: dict[str, tuple[str, ...]] = {
    "0": ("o",),
    "o": ("0",),
    "1": ("l", "i"),
    "l": ("1", "i"),
    "i": ("1", "l"),
    "5": ("s",),
    "s": ("5",),
    "8": ("b",),
    "b": ("8",),
    "2": ("z",),
    "z": ("2",),
}


def _ocr_variants(key: str) -> list[str]:
    """Single-character OCR repairs of ``key``."""
    variants: list[str] = []
    for index, char in enumerate(key):
        for replacement in _OCR_CONFUSIONS.get(char, ()):
            variants.append(key[:index] + replacement + key[index + 1 :])
    return variants


class InventoryRecord:
    """A catalog row, with the name that matched it."""

    __slots__ = ("item", "stock", "unit_price", "category", "matched_via")

    def __init__(
        self,
        item: str,
        stock: int,
        unit_price: float | None,
        category: str | None,
        matched_via: str,
    ) -> None:
        self.item = item
        self.stock = stock
        self.unit_price = unit_price
        self.category = category
        self.matched_via = matched_via


class Catalog:
    """Resolves document item names against the catalog."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._alias_cache: dict[str, str] | None = None

    def _aliases(self) -> dict[str, str]:
        if self._alias_cache is None:
            rows = self._conn.execute("SELECT alias, canonical_item FROM item_aliases")
            self._alias_cache = {normalize(r["alias"]): r["canonical_item"] for r in rows}
        return self._alias_cache

    @staticmethod
    def _to_record(row: sqlite3.Row, matched_via: str) -> InventoryRecord:
        return InventoryRecord(
            item=row["item"],
            stock=int(row["stock"]),
            unit_price=row["unit_price"],
            category=row["category"],
            matched_via=matched_via,
        )

    def _fetch(self, name: str) -> sqlite3.Row | None:
        """``sqlite3`` cursors are untyped upstream; one suppression per fetch."""
        return self._conn.execute(  # type: ignore[no-any-return]
            "SELECT * FROM inventory WHERE item = ?", (name,)
        ).fetchone()

    def lookup(self, raw_name: str) -> InventoryRecord | None:
        """Resolve a document's item name to a catalog row."""
        cleaned = raw_name.strip()
        if row := self._fetch(cleaned):
            return self._to_record(row, "exact")

        key = normalize(cleaned)
        catalog = {
            normalize(r["item"]): r["item"]
            for r in self._conn.execute("SELECT item FROM inventory")
        }

        if canonical := self._aliases().get(key):
            if row := self._fetch(canonical):
                return self._to_record(row, "alias")

        if key in catalog:
            if row := self._fetch(catalog[key]):
                return self._to_record(row, "normalized")

        # Strip trailing qualifiers: "WidgetA (rush order)" is still WidgetA,
        # ordered on a second line at a different price.
        if "(" in cleaned:
            bare = normalize(re.sub(r"\s*\([^)]*\)\s*", " ", cleaned))
            if bare and bare in catalog:
                if row := self._fetch(catalog[bare]):
                    return self._to_record(row, "qualifier-stripped")

        for candidate in _ocr_variants(key):
            if candidate in catalog:
                if row := self._fetch(catalog[candidate]):
                    return self._to_record(row, "ocr-repair")
        return None
