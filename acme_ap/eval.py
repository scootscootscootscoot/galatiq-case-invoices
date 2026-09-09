"""Evaluation harness.

Scores the pipeline against the golden expectations and reports the numbers that
matter operationally.

The gating metric is **false-pay rate**: invoices the system paid that the golden
set says should have been rejected. Overall accuracy is the wrong headline
because it averages a cheap mistake together with an expensive one -- wrongly
holding a good invoice costs a clerk five minutes, while wrongly paying a
fraudulent one costs the invoice amount and is not recoverable. Those are not
the same error and should not share a score.

Runs on the deterministic offline provider by default, so a regression is a
regression rather than model variance.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from acme_ap.config import get_settings
from acme_ap.db.migrations import apply as apply_migrations
from acme_ap.db.repository import Repository
from acme_ap.db.seed import seed as seed_inventory
from acme_ap.models import Outcome, RunResult
from acme_ap.service import process_invoice

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATIONS_PATH = REPO_ROOT / "tests" / "golden" / "expectations.yaml"
TOLERANCE = 0.01


def load_expectations(path: Path | None = None) -> dict[str, dict[str, object]]:
    """Read the golden set."""
    target = path or EXPECTATIONS_PATH
    return dict(yaml.safe_load(target.read_text(encoding="utf-8")))


def run_isolated(invoice_path: Path) -> RunResult:
    """Process one invoice in a throwaway database.

    Isolation is deliberate. Duplicate detection is stateful, so a shared
    database would make each result depend on the order the suite happened to
    run in. The 1004 duplicate scenario is asserted separately, on purpose.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "eval.db"
        apply_migrations(db)
        seed_inventory(db)
        repo = Repository(db)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return process_invoice(invoice_path, repo=repo)
        finally:
            repo.close()


@dataclass
class FileScore:
    """How one invoice fared against its expectation."""

    filename: str
    expected: str
    actual: str
    field_checks: int = 0
    field_hits: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def decision_correct(self) -> bool:
        return self.expected == self.actual

    @property
    def false_pay(self) -> bool:
        """Paid something the golden set says to reject. The expensive error."""
        return self.actual == Outcome.PAID.value and self.expected != Outcome.PAID.value

    @property
    def false_hold(self) -> bool:
        """Rejected something payable. The cheap error."""
        return self.actual != Outcome.PAID.value and self.expected == Outcome.PAID.value

    @property
    def clean(self) -> bool:
        return self.decision_correct and not self.failures


@dataclass
class Scorecard:
    """Aggregate results."""

    scores: list[FileScore] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def decisions_correct(self) -> int:
        return sum(1 for s in self.scores if s.decision_correct)

    @property
    def false_pays(self) -> list[FileScore]:
        return [s for s in self.scores if s.false_pay]

    @property
    def false_holds(self) -> list[FileScore]:
        return [s for s in self.scores if s.false_hold]

    @property
    def field_accuracy(self) -> float:
        checks = sum(s.field_checks for s in self.scores)
        hits = sum(s.field_hits for s in self.scores)
        return (hits / checks * 100.0) if checks else 100.0

    @property
    def decision_accuracy(self) -> float:
        return (self.decisions_correct / self.total * 100.0) if self.total else 100.0

    @property
    def false_pay_rate(self) -> float:
        return (len(self.false_pays) / self.total * 100.0) if self.total else 0.0

    @property
    def passed(self) -> bool:
        """CI gate: no false pays, and every decision correct."""
        return bool(self.scores) and all(score.clean for score in self.scores)


Check = tuple[str, object, object, bool]


def _numeric(value: object) -> float | None:
    """Turn a golden-set value into a comparable float, or bail out."""
    return float(value) if isinstance(value, (int, float)) else None


def _quantities_checks(result: RunResult, expected: dict[str, object]) -> list[Check]:
    """Compare aggregated quantities against the golden expectations."""
    if (want_q := expected.get("quantities")) is None:
        return []
    actual = result.invoice.aggregated_quantities() if result.invoice else {}
    rows: list[Check] = []
    for item, want in want_q.items() if isinstance(want_q, dict) else []:
        if not isinstance(item, str):
            continue
        quantity = _numeric(want)
        got = actual.get(item)
        passed = got is not None and quantity is not None and abs(got - quantity) <= TOLERANCE
        rows.append((f"quantity[{item}]", want, got, passed))
    return rows


def _flag_checks(result: RunResult, expected: dict[str, object]) -> list[Check]:
    """Check expected FindingCode presence/absence."""
    report = result.validation
    codes = {c.value for c in report.codes()} if report else set()
    rows: list[Check] = []
    for label_key in ("must_flag", "must_not_flag"):
        values = expected.get(label_key)
        for code in values if isinstance(values, list) else []:
            if not isinstance(code, str):
                continue
            should = label_key == "must_flag"
            passed = (code in codes) if should else (code not in codes)
            rows.append((f"{label_key}[{code}]", code, sorted(codes), passed))
    return rows


def _field_checks(result: RunResult, expected: dict[str, object]) -> list[Check]:
    """Compute per-field comparisons in data-driven order.

    Each expectation key maps to a (label, expected, actual, passed) check, so
    adding a new field is a dict entry instead of a branch.
    """
    invoice = result.invoice
    checks: list[Check] = []

    if (want_number := expected.get("invoice_number")) is not None:
        got_number = invoice.invoice_number if invoice else None
        checks.append(("invoice_number", want_number, got_number, got_number == want_number))

    if (want_vendor := expected.get("vendor")) is not None:
        got_vendor = invoice.vendor_name if invoice else None
        checks.append(("vendor", want_vendor, got_vendor, got_vendor == want_vendor))

    if (want_total := expected.get("total")) is not None:
        want = _numeric(want_total)
        got_total = invoice.total if invoice else None
        passed = got_total is not None and want is not None and abs(got_total - want) <= TOLERANCE
        checks.append(("total", want_total, got_total, passed))

    checks.extend(_quantities_checks(result, expected))
    checks.extend(_flag_checks(result, expected))
    return checks


def score_file(filename: str, expected: dict[str, object], result: RunResult) -> FileScore:
    """Compare one run against its expectation."""
    score = FileScore(
        filename=filename,
        expected=str(expected["outcome"]),
        actual=result.outcome.value,
    )
    if not score.decision_correct:
        score.failures.append(f"expected {score.expected}, got {score.actual}")

    for label, want, got, passed in _field_checks(result, expected):
        score.field_checks += 1
        if passed:
            score.field_hits += 1
        else:
            score.failures.append(f"{label}: expected {want!r}, got {got!r}")
    return score


def evaluate(expectations: dict[str, dict[str, object]] | None = None) -> Scorecard:
    """Run every invoice in the golden set and score it."""
    settings = get_settings()
    spec = expectations or load_expectations()
    card = Scorecard()
    for filename, expected in spec.items():
        path = settings.invoice_dir / filename
        if not path.exists():
            card.scores.append(
                FileScore(
                    filename=filename,
                    expected=str(expected["outcome"]),
                    actual="MISSING",
                    failures=[f"file not found: {path}"],
                )
            )
            continue
        card.scores.append(score_file(filename, expected, run_isolated(path)))
    return card


def render(card: Scorecard) -> None:
    """Print the scorecard."""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()

    table = Table(title="golden set", box=None, header_style="dim")
    table.add_column("invoice")
    table.add_column("expected")
    table.add_column("actual")
    table.add_column("fields", justify="right")
    table.add_column("notes")

    for score in card.scores:
        if score.clean:
            mark, style = "ok", "green"
        elif score.false_pay:
            mark, style = "FALSE PAY", "bold white on red"
        elif not score.decision_correct:
            mark, style = "wrong decision", "red"
        else:
            mark, style = "field mismatch", "yellow"
        table.add_row(
            score.filename,
            score.expected,
            score.actual,
            f"{score.field_hits}/{score.field_checks}",
            Text(mark, style=style),
        )
    console.print(table)

    for score in card.scores:
        if score.failures:
            console.print(f"\n[bold]{score.filename}[/]")
            for failure in score.failures:
                console.print(f"  [red]·[/] {failure}")

    summary = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
    summary.add_column(style="dim")
    summary.add_column(justify="right")
    summary.add_row("invoices scored", str(card.total))
    summary.add_row("decision accuracy", f"{card.decision_accuracy:.1f}%")
    summary.add_row("field accuracy", f"{card.field_accuracy:.1f}%")
    summary.add_row(
        "false-pay rate",
        Text(
            f"{card.false_pay_rate:.1f}%  ({len(card.false_pays)})",
            style="bold red" if card.false_pays else "bold green",
        ),
    )
    summary.add_row("false-hold rate", f"{len(card.false_holds) / max(card.total, 1) * 100:.1f}%")
    console.print()
    console.print(summary)
    console.print()
    console.print(
        Text(" PASS ", style="bold white on green")
        if card.passed
        else Text(" FAIL ", style="bold white on red")
    )


def main() -> int:
    from acme_ap.logging import configure_logging

    configure_logging("console")
    import logging

    logging.getLogger().setLevel(logging.ERROR)

    card = evaluate()
    render(card)
    return 0 if card.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
