"""Exercise the actual browser workflow and record a captioned MP4.

Uses an isolated database and copies of the sample documents. No real payments,
external models, or existing operator history are touched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "demo.mp4")
    parser.add_argument(
        "--fast", action="store_true", help="Exercise the flow without reading pauses"
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acme-demo-") as directory:
        scratch = Path(directory)
        invoice_dir = scratch / "invoices"
        shutil.copytree(
            ROOT / "data" / "invoices", invoice_dir, ignore=shutil.ignore_patterns("uploads")
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "DATABASE_PATH": str(scratch / "demo.db"),
            "INVOICE_DIR": str(invoice_dir),
            "LLM_PROVIDER": "stub",
            "LOG_LEVEL": "WARNING",
        }
        with (scratch / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    str(ROOT / ".venv" / "bin" / "python"),
                    "-m",
                    "uvicorn",
                    "acme_ap.api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(f"{url}/api/health", timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError((scratch / "server.log").read_text())
                record(url, scratch, args.output, args.fast)
            finally:
                server.terminate()
                server.wait(timeout=10)


def record(url: str, scratch: Path, output: Path, fast: bool) -> None:
    errors: list[str] = []
    checkpoints: list[dict[str, object]] = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            record_video_dir=str(scratch / "video"),
            record_video_size={"width": 1440, "height": 1000},
            reduced_motion="reduce",
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        expect(page.locator(".file")).to_have_count(20)
        page.evaluate("""() => {
            const caption = document.createElement('div'); caption.id = 'demo-caption';
            caption.style.cssText = 'position:fixed;bottom:20px;left:40px;right:40px;padding:18px 24px;background:#071319f5;border:1px solid #4fd1c5;border-radius:10px;color:#dbe4ee;font:17px/1.5 system-ui;z-index:1000;box-shadow:0 8px 40px #0008;pointer-events:none';
            document.body.appendChild(caption);
        }""")

        def caption(title: str, detail: str, seconds: float = 4) -> None:
            page.locator("#demo-caption").evaluate(
                "(node, text) => { node.replaceChildren(); const title=document.createElement('strong'); title.style.color='#4fd1c5'; title.textContent=text[0]; node.append(title, document.createElement('br'), document.createTextNode(text[1])); }",
                [title, detail],
            )
            checkpoints.append({"seconds": round(time.monotonic() - started, 1), "chapter": title})
            print(title, flush=True)
            page.wait_for_timeout(120 if fast else seconds * 1000)

        def run(filename: str, outcome: str) -> None:
            page.locator(f'.file[data-name="{filename}"]').click()
            expect(page.locator("#result h3")).to_have_text(outcome, timeout=30000)

        caption(
            "Acme AP · trust the evidence before paying",
            "Five invoice formats, source checks, durable review alerts, and simulated payments. This is a real browser session with the offline provider.",
            6,
        )
        run("invoice_1011.pdf", "PAID")
        caption(
            "1 · A readable PDF clears the checks",
            "The extracted invoice matches the source, stock covers the quantities, and the total reconciles. The audit trail records every step.",
            6,
        )
        page.locator("#quality").scroll_into_view_if_needed()
        page.locator(".evidence summary").click()
        page.locator("#quality").evaluate(
            "node => window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - 100)"
        )
        expect(page.locator("#quality")).to_contain_text("Page 1")
        page.screenshot(path=str(output.parent / "source-evidence.png"))
        caption(
            "Every confidence score has evidence",
            "Expand the field checks to see the extracted value, the source excerpt, and its page. Scores are rule-based evidence ratings, not probabilities.",
            6,
        )
        page.evaluate("window.scrollTo(0,0)")
        run("invoice_1012.pdf", "PAID")
        page.locator("#quality").scroll_into_view_if_needed()
        caption(
            "2 · Damaged characters stay inspectable",
            "The sample contains capital O characters where zeros belong. Deterministic normalization repairs them and retains the original text.",
            6,
        )
        page.evaluate("window.scrollTo(0,0)")
        run("invoice_1006.csv", "REVIEW_REQUIRED")
        caption(
            "3 · Missing currency means stop and ask a human",
            "This CSV never specifies a currency. A score of 85 falls below the 90 threshold. No payment is issued, and a persistent review alert opens.",
            7,
        )
        page.locator("#tab-review").click()
        expect(page.locator("#reviews")).to_contain_text("INV-1006")
        page.screenshot(path=str(output.parent / "review-queue.png"))
        caption(
            "A queue that survives a page reload",
            "The alert is stored with the run in SQLite. Operators can reopen the evidence and resolve it with an attributable note.",
            5,
        )
        page.reload()
        page.locator("#tab-review").click()
        expect(page.locator("#reviews")).to_contain_text("INV-1006")
        page.locator(".review-open").click()
        expect(page.locator("#review-workspace")).to_be_visible()
        page.locator("#reviewer-name").fill("Demo Reviewer")
        page.locator("#review-note").fill(
            "Demo verification: confirmed USD with the supplier and checked the invoice fields and every line."
        )
        page.locator("#source-verified").check()
        page.screenshot(path=str(output.parent / "manual-review.png"))
        # A reload removes the recording-only captions; recreate once.
        page.evaluate(
            """() => { if (!document.getElementById('demo-caption')) { const n=document.createElement('div'); n.id='demo-caption'; n.style.cssText='position:fixed;bottom:20px;left:40px;right:40px;padding:18px 24px;background:#071319f5;border:1px solid #4fd1c5;border-radius:10px;color:#dbe4ee;font:17px/1.5 system-ui;z-index:1000;pointer-events:none'; document.body.append(n); } }"""
        )
        caption(
            "4 · Human verification is recorded, not hidden",
            "The reviewer verifies the source and records a reason. This demonstration simulates supplier confirmation of the missing currency.",
            6,
        )
        page.locator("#submit-review").click()
        expect(page.locator("#result h3")).to_have_text("PAID", timeout=30000)
        page.locator("#quality").scroll_into_view_if_needed()
        expect(page.locator("#quality")).to_contain_text("Verified by a reviewer")
        caption(
            "A new run rechecks the corrected invoice",
            "Business rules still apply. The original hold remains in history, linked to the correction. The machine score stays at 85; human verification is explicit.",
            7,
        )
        page.evaluate("window.scrollTo(0,0)")
        run("invoice_1013.pdf", "REJECTED")
        expect(page.locator("#findings")).to_contain_text("STOCK_EXCEEDED")
        expect(page.locator("#findings")).to_contain_text("ARITHMETIC_MISMATCH")
        caption(
            "5 · Business errors still block payment",
            "Repeated products are aggregated before the stock check. This invoice also has an unexplained $50 difference. The approval model cannot override the block.",
            7,
        )
        run("invoice_1004.json", "PAID")
        run("invoice_1004_revised.json", "REJECTED")
        expect(page.locator("#findings")).to_contain_text("DUPLICATE_INVOICE")
        caption(
            "6 · A revised invoice cannot be paid twice",
            "The original and revision share an invoice number. A database reservation also protects simultaneous requests before the mock payment is called.",
            6,
        )
        from tests.test_ocr import scanned_invoice

        scan = scratch / "scan.pdf"
        scanned_invoice(scan)
        page.locator("#upload-file").set_input_files(scan)
        expect(page.locator("#result h3")).to_have_text("REVIEW_REQUIRED", timeout=30000)
        page.locator("#quality").scroll_into_view_if_needed()
        expect(page.locator("#quality")).to_contain_text("OCR transcription")
        caption(
            "7 · An actual image-only PDF",
            "Local Tesseract reads the scanned page. OCR is conservatively routed to review until its accuracy is validated on a broader corpus.",
            7,
        )
        page.locator("#tab-history").click()
        expect(page.locator("#history")).to_contain_text("INV-8830")
        caption(
            "An inspectable history from upload to outcome",
            "Payments, rejections, holds, corrections, and reasons share one audit trail. The repository includes adversarial tests and this repeatable recording script.",
            7,
        )
        assert not errors, errors
        video = page.video
        page.close()
        context.close()
        assert video is not None
        raw_video = Path(video.path())
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(raw_video),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
        )
        mobile_context = browser.new_context(viewport={"width": 390, "height": 844})
        mobile = mobile_context.new_page()
        mobile.goto(url)
        expect(mobile.locator("#tab-review")).to_be_visible()
        mobile.locator("#tab-review").click()
        expect(mobile.locator(".review-open").first).to_be_visible()
        mobile.screenshot(path=str(output.parent / "mobile-review.png"), full_page=True)
        mobile_context.close()
        browser.close()
    (output.parent / "demo-chapters.json").write_text(json.dumps(checkpoints, indent=2) + "\n")
    print(f"PASS: browser workflow, zero JavaScript errors. Recorded {output}", flush=True)


if __name__ == "__main__":
    main()
