"""Bounded, local OCR. OCR output is evidence to review, never payment authority."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL.Image import Image


def recognize(image: Image, timeout: int) -> tuple[str, str | None]:
    executable = shutil.which("tesseract")
    if not executable:
        return "", "Tesseract is unavailable; install it or transcribe this page manually."
    with tempfile.TemporaryDirectory(prefix="acme-ocr-") as directory:
        source = Path(directory) / "page.png"
        image.save(source)
        try:
            result = subprocess.run(
                [executable, str(source), "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return result.stdout, None
        except (subprocess.SubprocessError, OSError) as exc:
            return "", f"OCR could not read this page ({type(exc).__name__})."
