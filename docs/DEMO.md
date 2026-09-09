# Recorded browser walkthrough

[Watch the MP4](demo.mp4), or open [the visual explainer](EXPLAINER.html), which
includes a video player. This is a recording of the actual application in
Chromium with captions. There is no audio narration.

The walkthrough processes a native PDF, shows source evidence, normalizes the
damaged text in `1012.pdf`, holds a currency-free CSV, opens the durable review
queue, records human verification, revalidates in a new run, rejects the `1013`
stock/arithmetic failure, blocks a duplicate revision, and uploads an actual
image-only PDF for local OCR and review.

It uses the offline provider, fictional invoices, and simulated payments. The
review note explicitly simulates a supplier confirming the missing currency.
No claims are made about a real supplier interaction or live model accuracy.

## Reproduce

```bash
.venv/bin/pip install -e '.[dev,demo]'
.venv/bin/python -m playwright install chromium
sudo apt-get install ffmpeg tesseract-ocr tesseract-ocr-eng fonts-dejavu-core
.venv/bin/python scripts/record_demo.py
```

The script starts an isolated local server, uses a temporary database and copied
samples, asserts each expected UI outcome, checks for JavaScript errors, and
exports an MP4, screenshots, and [chapter timing data](demo-chapters.json).
It also opens the review queue at a 390px mobile viewport.

Use `--fast --output artifacts/browser-check/demo.mp4` to exercise the same flow
without the caption reading pauses. The final video uses the normal reading pace.
The existing application database and uploaded documents are never reset.
