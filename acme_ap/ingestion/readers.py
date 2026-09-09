"""Turn a file of any supported format into plain text.

Deterministic work happens here, before any model is involved. A CSV parser
reads a CSV perfectly, for free, every time; asking an LLM to do it costs money,
introduces variance, and is untestable. The model's job starts where the
ambiguity starts, not before.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from acme_ap.config import get_settings
from acme_ap.logging import get_logger
from acme_ap.models import RawDocument

logger = get_logger(__name__)


class UnsupportedFormatError(ValueError):
    """Raised for a file extension we have no reader for."""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_txt(path: Path) -> tuple[str, dict[str, object]]:
    """Plain text, passed through untouched."""
    return _read_text(path), {}


def read_json(path: Path) -> tuple[str, dict[str, object]]:
    """JSON, re-serialised with stable indentation.

    Pretty-printing rather than passing the raw bytes gives the model a
    predictable shape regardless of how the vendor's system formatted it.
    """
    raw = _read_text(path)
    duplicates: list[str] = []

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in items:
            if key in parsed:
                duplicates.append(key)
            parsed[key] = value
        return parsed

    try:
        parsed = json.loads(raw, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        logger.warning("json parse failed, passing through raw", extra={"error": str(exc)})
        return raw, {"json_valid": False}
    return json.dumps(parsed, indent=2, sort_keys=False), {
        "json_valid": True,
        "quality_issues": [f"Repeated JSON key: {key}" for key in sorted(set(duplicates))],
    }


def read_csv(path: Path) -> tuple[str, dict[str, object]]:
    """CSV, rendered as aligned columns.

    The sample data contains two incompatible CSV dialects -- a two-column
    field/value layout and a wide tabular one -- so no schema is assumed. Both
    are flattened to readable text and the model resolves the shape.
    """
    raw = _read_text(path)
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return raw, {"csv_rows": 0}

    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    col_widths = [max(len(row[i]) for row in padded) for i in range(width)]
    lines = [
        "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in padded
    ]
    is_key_value = width == 2 and rows[0][:2] in (["field", "value"], ["Field", "Value"])
    return "\n".join(lines), {"csv_rows": len(rows), "csv_key_value": is_key_value}


def read_xml(path: Path) -> tuple[str, dict[str, object]]:
    """XML, flattened to indented ``path: value`` lines.

    Tag soup confuses extraction more than it helps; a flat outline of the
    document's actual content does not.
    """
    raw = _read_text(path)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning("xml parse failed, passing through raw", extra={"error": str(exc)})
        return raw, {"xml_valid": False}

    lines: list[str] = []

    def walk(node: ET.Element, depth: int) -> None:
        text = (node.text or "").strip()
        indent = "  " * depth
        lines.append(f"{indent}{node.tag}: {text}" if text else f"{indent}{node.tag}:")
        for child in node:
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines), {"xml_valid": True}


def read_pdf(path: Path) -> tuple[str, dict[str, object]]:
    """Read each page exactly once; retain page provenance and missing-page alerts."""
    import pdfplumber

    from acme_ap.ingestion.ocr import recognize

    chunks: list[str] = []
    tables_found = 0
    pages: list[dict[str, object]] = []
    issues: list[str] = []
    settings = get_settings()
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        if page_count > settings.max_pdf_pages:
            raise ValueError(f"PDF exceeds the {settings.max_pdf_pages}-page limit")
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            method = "native"
            tables_found += len(page.find_tables())
            if len(text.strip()) < 30:
                method = "ocr"
                try:
                    # Bound render size even for unusually large page dimensions.
                    resolution = min(200, 2400 * 72 / max(page.width, page.height))
                    text, error = recognize(
                        page.to_image(resolution=resolution).original,
                        settings.ocr_timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - retain unreadable-page evidence
                    text, error = "", f"Page rendering failed ({type(exc).__name__})."
                issues.append(
                    f"Page {number}: {error or 'OCR transcription requires verification.'}"
                )
            elif any(
                float(img.get("width", 0)) * float(img.get("height", 0))
                > float(page.width * page.height) * 0.25
                for img in page.images
            ):
                issues.append(
                    f"Page {number}: substantial image content may be absent from the text layer."
                )
            if not text.strip():
                method = "unreadable"
                issues.append(f"Page {number}: no readable text; page coverage is incomplete.")
            pages.append({"page": number, "method": method, "text": text})
            chunks.append(text)
    return "\n\f\n".join(chunks), {
        "pdf_pages": page_count,
        "pdf_tables": tables_found,
        "pages": pages,
        "quality_issues": issues,
    }


READERS: dict[str, Callable[[Path], tuple[str, dict[str, object]]]] = {
    ".txt": read_txt,
    ".text": read_txt,
    ".json": read_json,
    ".csv": read_csv,
    ".xml": read_xml,
    ".pdf": read_pdf,
}


def load_document(path: str | Path) -> RawDocument:
    """Read ``path`` into a :class:`RawDocument`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such invoice: {p}")

    suffix = p.suffix.lower()
    reader = READERS.get(suffix)
    if reader is None:
        raise UnsupportedFormatError(
            f"no reader for '{suffix}' (supported: {', '.join(sorted(READERS))})"
        )

    text, metadata = reader(p)
    metadata["file_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    if not text.strip() and suffix != ".pdf":
        raise ValueError(f"{p.name} produced no readable text")

    logger.info(
        "document loaded",
        extra={"path": str(p), "format": suffix.lstrip("."), "chars": len(text)},
    )
    return RawDocument(
        source_path=str(p),
        source_format=suffix.lstrip("."),
        text=text,
        metadata=metadata,
    )


def supported_extensions() -> list[str]:
    return sorted(READERS)
