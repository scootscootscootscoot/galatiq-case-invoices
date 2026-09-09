"""Conservative source corroboration, separate from business validity.

Scores describe checks passed, not calibrated probabilities of correctness.
The offline extractor shares parsing code with this verifier: agreement alone
is not independent accuracy evidence. Unrecognized layouts and OCR are held.
"""

from __future__ import annotations

import json
import re
from datetime import date

from acme_ap.ingestion.heuristics import parse_document
from acme_ap.ingestion.heuristics.text import _LABELS
from acme_ap.models import ExtractedInvoice, ExtractionQuality, FieldEvidence, RawDocument

_HEADERS = (
    "invoice_number",
    "vendor_name",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_amount",
    "shipping",
    "total",
)
_REQUIRED = {"invoice_number", "vendor_name", "due_date", "currency", "total"}
_ROW_FIELDS = ("raw_name", "quantity", "unit_price", "amount")
_OCR = re.compile(r"\d[\d,.]*[OoIlS][\d,.]*|[OoIlS]\d")


def _display(value: object) -> str | None:
    return None if value is None else str(value)


def _same(left: object, right: object) -> bool:
    if isinstance(left, int | float) and isinstance(right, int | float):
        return abs(left - right) < 0.000001
    if isinstance(left, str) and isinstance(right, str):
        return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()
    return left == right


def _excerpt(
    document: RawDocument, needle: str | None, occurrence: int = 0
) -> tuple[int | None, str | None]:
    if not needle:
        return None, None
    for page, text in enumerate(document.text.split("\f"), 1):
        for line in text.splitlines():
            if needle.casefold() in line.casefold():
                if occurrence:
                    occurrence -= 1
                    continue
                return (page if document.source_format == "pdf" else None), line.strip()[:300]
    return None, None


def _evidence(
    document: RawDocument,
    field: str,
    value: object,
    source: object,
    *,
    required: bool = False,
    needle: str | None = None,
    occurrence: int = 0,
) -> FieldEvidence:
    page, excerpt = None, None
    if field in _HEADERS:
        aliases = _LABELS.get(
            {"vendor_name": "vendor", "invoice_date": "date", "tax_amount": "tax"}.get(
                field, field
            ),
            (field,),
        )
        for page_number, page_text in enumerate(document.text.split("\f"), 1):
            for line in page_text.splitlines():
                if any(
                    re.match(
                        r'^\s*"?'
                        + re.escape(alias).replace(r"\ ", r"[ _]")
                        + r'"?(?:\s*[:=]|\s{2,})',
                        line,
                        re.I,
                    )
                    for alias in aliases
                ):
                    page = page_number if document.source_format == "pdf" else None
                    excerpt = line.strip()[:300]
                    break
            if excerpt:
                break
    if not excerpt:
        page, excerpt = _excerpt(document, needle or _display(source), occurrence)
    if value is None or (isinstance(value, str) and not value.strip()):
        score, reason = (
            (0.0, "Required value is missing") if required else (0.5, "Source value was omitted")
        )
    elif source is None:
        score, reason = 0.4, "Value cannot be corroborated by a recognized source field"
    elif not _same(value, source):
        score, reason = 0.0, "Extraction disagrees with the source field"
    else:
        score, reason = 0.99, "Matches the recognized source field"
        if excerpt and _OCR.search(excerpt) and not field.endswith("raw_name"):
            score, reason = (
                0.92,
                "Matches after deterministic OCR-character repair; inspect the original",
            )
    return FieldEvidence(
        field=field,
        value=_display(value),
        source_value=_display(source),
        score=score,
        reason=reason,
        page=page,
        excerpt=excerpt,
    )


def _ambiguities(document: RawDocument) -> list[str]:
    text = document.text
    issues: list[str] = []
    raw_issues = document.metadata.get("quality_issues", [])
    if isinstance(raw_issues, list):
        issues.extend(str(issue) for issue in raw_issues)
    for key in ("json_valid", "xml_valid"):
        if document.metadata.get(key) is False:
            issues.append(f"Invalid {key.split('_')[0].upper()} structure")
    if re.search(r"\b\d{1,3}\.\d{3},\d{2}\b|\b\d+,\d{2}\b(?!\d)", text):
        issues.append("Comma-decimal money is ambiguous under the supported US number format")
    for match in re.finditer(r"\b(\d{1,2})/(\d{1,2})/\d{4}\b", text):
        a, b = int(match[1]), int(match[2])
        if 1 <= a <= 12 and 1 <= b <= 12 and a != b:
            issues.append(f"Ambiguous day/month order: {match[0]}")
    # Multiple conflicting labelled totals cannot be resolved by choosing the first.
    totals = re.findall(
        r"(?im)^\s*(?:grand\s+total|total(?:\s+amount)?|amount\s+due)\s*[:=]\s*(.+)$", text
    )
    if len({t.strip() for t in totals}) > 1:
        issues.append("Conflicting invoice totals appear in the source")
    if "\ufffd" in text:
        issues.append("Source text contains undecodable characters")
    codes = set(re.findall(r"\b(?:USD|EUR|GBP|CAD|JPY|AUD|CHF)\b", text))
    if len(codes) > 1:
        issues.append("Multiple currency codes appear in the source")
    return list(dict.fromkeys(issues))


def _coverage_issues(document: RawDocument, source: ExtractedInvoice) -> list[str]:
    """Catch rows dropped by a permissive parser, including unsupported shapes."""
    if document.source_format == "json":
        try:
            data = json.loads(document.text)
        except ValueError:
            return []
        if isinstance(data, dict):
            rows = data.get("line_items") or data.get("items") or []
            if not isinstance(rows, list) or len(rows) != len(source.line_items):
                return [
                    "Some structured line items were not extracted; source coverage is incomplete"
                ]
    if document.source_format in {"txt", "text", "pdf"}:
        for line in document.text.splitlines():
            # An unfamiliar priced row in the item table is unsafe to ignore.
            stripped = line.strip()
            if not re.match(r"^[A-Za-z]", stripped) or ":" in stripped:
                continue
            if len(re.findall(r"[$€£]\s*\d", stripped)) < 2:
                continue
            if not any(
                stripped.casefold().startswith(item.raw_name.casefold())
                for item in source.line_items
            ):
                return [f"Unrecognized priced row: {stripped[:150]}"]
    return []


def assess(
    document: RawDocument,
    invoice: ExtractedInvoice,
    threshold: float,
    problems: list[str] | None = None,
    *,
    human_verified: bool = False,
) -> ExtractionQuality:
    """Weakest required field wins; a good vendor cannot hide an uncertain total."""
    fields: list[FieldEvidence] = []
    issues = _ambiguities(document)
    try:
        source = parse_document(document.text, document.source_format)
    except (ValueError, TypeError, AttributeError, KeyError):
        source = ExtractedInvoice()
        issues.append("Source structure is unsupported by the deterministic verifier")
    issues.extend(_coverage_issues(document, source))
    for name in _HEADERS:
        value, expected = getattr(invoice, name), getattr(source, name)
        if name not in _REQUIRED and value is None and expected is None:
            continue
        needle = None
        if isinstance(expected, date):
            needle = source.raw_due_date_text if name == "due_date" else source.raw_date_text
        evidence = _evidence(
            document, name, value, expected, required=name in _REQUIRED, needle=needle
        )
        if name == "currency" and not re.search(
            r"\b(?:USD|EUR|GBP|CAD|JPY|AUD|CHF)\b|[$€£]", document.text
        ):
            evidence.score = 0.85
            evidence.reason = "Currency was defaulted; no explicit code or symbol was found"
        fields.append(evidence)
    fields.append(
        _evidence(
            document,
            "line_items.count",
            len(invoice.line_items),
            len(source.line_items),
            required=True,
        )
    )
    if not invoice.line_items:
        fields[-1].score, fields[-1].reason = 0.0, "No line items were read"
    for index, item in enumerate(invoice.line_items):
        source_item = source.line_items[index] if index < len(source.line_items) else None
        for name in _ROW_FIELDS:
            value, expected = getattr(item, name), getattr(source_item, name, None)
            if (
                name in {"unit_price", "amount"}
                and value is None
                and expected is None
                and (item.amount is not None or name == "amount")
            ):
                continue
            fields.append(
                _evidence(
                    document,
                    f"line_items[{index}].{name}",
                    value,
                    expected,
                    required=name in {"raw_name", "quantity"}
                    or (name == "unit_price" and item.amount is None),
                    needle=source_item.raw_name if source_item else item.raw_name,
                    occurrence=sum(
                        previous.raw_name == item.raw_name
                        for previous in invoice.line_items[:index]
                    ),
                )
            )
    for problem in problems or []:
        issues.append(f"Extraction critique unresolved: {problem}")
    score = min((field.score for field in fields), default=0.0)
    if issues:
        score = min(score, 0.65)
    reasons = [f"{field.field}: {field.reason}" for field in fields if field.score < threshold]
    reasons.extend(issues)
    return ExtractionQuality(
        score=score,
        threshold=threshold,
        fields=fields,
        reasons=reasons,
        requires_review=bool(score < threshold or issues) and not human_verified,
        human_verified=human_verified,
    )
