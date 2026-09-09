"""Command-line interface.

The invocation the case specifies works verbatim::

    python main.py --invoice_path=data/invoices/invoice_1001.txt

Output is a readable trace of what each agent did and why, because the point of
an automated approval is that a human can audit the reasoning afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from acme_ap.config import get_settings
from acme_ap.logging import configure_logging
from acme_ap.models import Outcome, RunResult, Severity
from acme_ap.service import ensure_database, list_invoice_files, process_invoice

app = typer.Typer(
    add_completion=False,
    help="Acme Corp accounts-payable pipeline.",
    no_args_is_help=True,
)
console = Console()

_SEVERITY_STYLE = {
    Severity.INFO: "dim cyan",
    Severity.WARN: "yellow",
    Severity.BLOCK: "bold red",
}
_OUTCOME_STYLE = {
    Outcome.PAID: "bold green",
    Outcome.REJECTED: "bold red",
    Outcome.FAILED: "bold magenta",
    Outcome.REVIEW_REQUIRED: "bold yellow",
}


def _render_extraction(result: RunResult) -> None:
    """Section: the extracted invoice."""
    invoice = result.invoice
    if not invoice:
        return
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("invoice", invoice.invoice_number or "[dim]—[/]")
    table.add_row("vendor", invoice.vendor_name or "[dim]—[/]")
    table.add_row("dated", str(invoice.invoice_date or "—"))
    table.add_row("due", str(invoice.due_date or "—"))
    table.add_row(
        "total",
        f"{invoice.currency} {invoice.total:,.2f}" if invoice.total is not None else "—",
    )
    table.add_row("attempts", str(result.extraction_attempts))
    if result.quality:
        quality = result.quality
        table.add_row("source evidence", f"{quality.score:.0%} / threshold {quality.threshold:.0%}")
        if quality.requires_review:
            table.add_row("manual review", "Alert opened — inspect the dashboard review queue")
    console.print(Panel(table, title="extracted", border_style="cyan", title_align="left"))

    if not invoice.line_items:
        return
    items = Table(box=None, pad_edge=False, header_style="dim")
    items.add_column("item")
    items.add_column("resolved", style="cyan")
    items.add_column("qty", justify="right")
    items.add_column("unit", justify="right")
    items.add_column("amount", justify="right")
    for li in invoice.line_items:
        items.add_row(
            li.raw_name,
            li.canonical_item or "[red]unresolved[/]",
            f"{li.quantity:g}" if li.quantity is not None else "—",
            f"{li.unit_price:,.2f}" if li.unit_price is not None else "—",
            f"{(li.amount if li.amount is not None else li.computed_amount) or 0:,.2f}",
        )
    console.print(items)

    aggregated = invoice.aggregated_quantities()
    if len(aggregated) < len(invoice.line_items):
        merged = ", ".join(f"{k} x{v:g}" for k, v in aggregated.items())
        console.print(f"  [dim]aggregated for stock check:[/] {merged}")


def _render_validation(result: RunResult) -> None:
    """Section: validation findings."""
    report = result.validation
    if not report:
        return
    if not report.findings:
        console.print(
            Panel("No findings.", title="validation", border_style="blue", title_align="left")
        )
        return
    findings = Table(box=None, pad_edge=False, header_style="dim")
    findings.add_column("severity")
    findings.add_column("code")
    findings.add_column("detail")
    ordered = sorted(report.findings, key=lambda f: list(Severity).index(f.severity), reverse=True)
    for finding in ordered:
        findings.add_row(
            Text(finding.severity.value, style=_SEVERITY_STYLE[finding.severity]),
            finding.code.value,
            finding.message,
        )
    console.print(Panel(findings, title="validation", border_style="blue", title_align="left"))


def _render_approval(result: RunResult, *, verbose: bool) -> None:
    """Section: the VP decision with critique rounds."""
    decision = result.approval
    if not decision:
        return
    body = Text(decision.rationale)
    if verbose and decision.critique_rounds:
        for round_ in decision.critique_rounds:
            body.append(
                f"\n\nround {round_.round_number} ({'approve' if round_.decision else 'reject'}): ",
                style="dim",
            )
            body.append(round_.proposal)
            if round_.critique:
                body.append(
                    f"\n  reviewer {'agreed' if round_.accepted else 'objected'}: ",
                    style="dim",
                )
                body.append(round_.critique)
    title = "approval" if decision.approved else "rejection"
    if decision.hard_gate_triggered:
        title += " · policy gate"
    console.print(
        Panel(
            body,
            title=title,
            border_style="green" if decision.approved else "red",
            title_align="left",
            subtitle=f"policy {decision.policy_version} · "
            f"{len(decision.critique_rounds)} critique round(s)",
        )
    )


def _render_payment(result: RunResult) -> None:
    """Section: the payment receipt."""
    if result.payment:
        console.print(
            Panel(
                f"{result.payment.status} · {result.payment.currency} "
                f"{result.payment.amount:,.2f} to {result.payment.vendor}\n"
                f"[dim]idempotency key {result.payment.idempotency_key}[/]"
                + (f"\n{result.payment.detail}" if result.payment.detail else ""),
                title="payment",
                border_style="green",
                title_align="left",
            )
        )


def _render(result: RunResult, *, verbose: bool) -> None:
    """Print the full story of a run, section by section."""
    console.print()
    console.print(Rule(f"[bold]{Path(result.source_path).name}[/]", style="cyan"))

    if result.degraded:
        console.print(
            Panel(
                "No xAI key configured — running on the deterministic offline provider. "
                "Set XAI_API_KEY in .env for live model reasoning.",
                title="degraded",
                border_style="yellow",
            )
        )
    if result.error:
        console.print(Panel(result.error, title="error", border_style="magenta"))

    _render_extraction(result)
    _render_validation(result)
    _render_approval(result, verbose=verbose)
    _render_payment(result)

    style = _OUTCOME_STYLE[result.outcome]
    console.print(Rule(Text(result.outcome.value, style=style), style=style))
    console.print(f"[dim]run {result.run_id} · {result.provider}/{result.model}[/]")
    console.print()


@app.command()
def run(
    invoice_path: Annotated[
        Path,
        typer.Option(
            "--invoice_path",
            "--invoice-path",
            "-i",
            help="Path to the invoice document.",
        ),
    ],
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every critique round.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the result as JSON instead of a report.")
    ] = False,
) -> None:
    """Process one invoice end to end."""
    configure_logging("json" if json_output else "console")
    ensure_database()

    result = process_invoice(invoice_path)

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render(result, verbose=verbose)

    # Non-zero for anything that did not pay, so the CLI composes in a shell.
    raise typer.Exit(code=0 if result.outcome is Outcome.PAID else 1)


@app.command("list")
def list_invoices() -> None:
    """List the invoice documents available to process."""
    configure_logging("console")
    files = list_invoice_files()
    table = Table(title=f"{len(files)} invoice document(s)", box=None, header_style="dim")
    table.add_column("file")
    table.add_column("format")
    table.add_column("bytes", justify="right")
    for entry in files:
        table.add_row(str(entry["name"]), str(entry["format"]), f"{entry['bytes']:,}")
    console.print(table)


@app.command()
def inventory() -> None:
    """Show the current inventory catalog."""
    from acme_ap.db.repository import Repository

    configure_logging("console")
    ensure_database()
    repo = Repository(get_settings().database_path)
    table = Table(title="inventory", box=None, header_style="dim")
    table.add_column("item")
    table.add_column("stock", justify="right")
    table.add_column("unit price", justify="right")
    table.add_column("category", style="dim")
    for row in repo.all_inventory():
        table.add_row(
            row["item"],
            str(row["stock"]),
            f"{row['unit_price']:,.2f}" if row["unit_price"] is not None else "—",
            row["category"] or "",
        )
    repo.close()
    console.print(table)


@app.command()
def history(limit: int = 20) -> None:
    """Show recent runs from the system of record."""
    from acme_ap.db.repository import Repository

    configure_logging("console")
    ensure_database()
    repo = Repository(get_settings().database_path)
    rows = repo.list_runs(limit)
    repo.close()

    table = Table(title=f"last {len(rows)} run(s)", box=None, header_style="dim")
    table.add_column("started", style="dim")
    table.add_column("invoice")
    table.add_column("vendor")
    table.add_column("total", justify="right")
    table.add_column("outcome")
    for row in rows:
        outcome = row["status"]
        style = {"PAID": "green", "REJECTED": "red"}.get(outcome, "magenta")
        table.add_row(
            str(row["started_at"]),
            row["invoice_number"] or "—",
            (row["vendor_name"] or "—")[:28],
            f"{row['total']:,.2f}" if row["total"] is not None else "—",
            Text(outcome, style=style),
        )
    console.print(table)


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete all run history and payments, keeping the inventory catalog.

    Payment history is deliberately durable -- duplicate detection depends on it,
    so re-running a paid invoice blocks rather than paying twice. That is correct
    behaviour and not something to work around, which is why clearing it is an
    explicit, confirmed act rather than a flag on every run.
    """
    settings = get_settings()
    if not yes:
        confirm = typer.confirm(f"Delete all run history in {settings.database_path}?")
        if not confirm:
            console.print("[yellow]cancelled[/]")
            raise typer.Exit(code=1)

    from acme_ap.db.migrations import connect

    conn = connect(settings.database_path)
    for table in (
        "reviews",
        "extraction_records",
        "payment_claims",
        "payments",
        "decisions",
        "validation_findings",
        "line_items",
        "invoices",
        "agent_events",
        "runs",
    ):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed identifier list
    conn.commit()
    conn.close()
    console.print("[green]run history cleared[/] (inventory preserved)")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
