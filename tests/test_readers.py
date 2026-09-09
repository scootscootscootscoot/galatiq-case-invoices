"""Format readers: deterministic, no model involved."""

from __future__ import annotations

from pathlib import Path

import pytest

from acme_ap.ingestion.readers import (
    UnsupportedFormatError,
    load_document,
    supported_extensions,
)

ALL_FORMATS = [
    ("invoice_1001.txt", "txt"),
    ("invoice_1004.json", "json"),
    ("invoice_1006.csv", "csv"),
    ("invoice_1014.xml", "xml"),
    ("invoice_1011.pdf", "pdf"),
]


@pytest.mark.parametrize(("filename", "expected_format"), ALL_FORMATS)
def test_every_format_yields_text(invoice_dir: Path, filename: str, expected_format: str) -> None:
    doc = load_document(invoice_dir / filename)
    assert doc.source_format == expected_format
    assert doc.text.strip()
    assert len(doc.content_hash) == 16


def test_all_sample_invoices_load(invoice_dir: Path) -> None:
    """Every file the case ships must be readable. No exceptions."""
    files = sorted(p for p in invoice_dir.iterdir() if p.suffix.lower() in supported_extensions())
    assert len(files) >= 17 and all(load_document(path).text.strip() for path in files)


def test_content_hash_is_stable_and_distinct(invoice_dir: Path) -> None:
    first = load_document(invoice_dir / "invoice_1001.txt")
    again = load_document(invoice_dir / "invoice_1001.txt")
    other = load_document(invoice_dir / "invoice_1002.txt")
    assert first.content_hash == again.content_hash
    assert first.content_hash != other.content_hash


def test_pdf_preserves_ocr_artifacts(invoice_dir: Path) -> None:
    """The reader must not 'helpfully' clean up damage.

    Repair is the extraction critique loop's job, and it can only repair what it
    can see. A reader that silently normalised these characters would hide the
    defect instead of surfacing it.
    """
    text = load_document(invoice_dir / "invoice_1012.pdf").text
    assert "2O26" in text
    assert "$3,500.O0" in text


def test_csv_dialects_both_readable(invoice_dir: Path) -> None:
    key_value = load_document(invoice_dir / "invoice_1006.csv")
    tabular = load_document(invoice_dir / "invoice_1007.csv")
    assert (
        key_value.metadata["csv_key_value"] is True,
        tabular.metadata["csv_key_value"] is False,
        "WidgetA" in key_value.text,
        "MegaWidgets" in tabular.text,
    ) == (True, True, True, True)


def test_xml_flattened_to_readable_lines(invoice_dir: Path) -> None:
    doc = load_document(invoice_dir / "invoice_1014.xml")
    assert "invoice_number: INV-1014" in doc.text
    assert "EUR" in doc.text


def test_unsupported_extension_is_explicit(tmp_path: Path) -> None:
    bad = tmp_path / "invoice.docx"
    bad.write_text("nope")
    with pytest.raises(UnsupportedFormatError, match="no reader"):
        load_document(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_document(tmp_path / "absent.txt")
