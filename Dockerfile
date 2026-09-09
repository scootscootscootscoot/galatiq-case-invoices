# Single-stage: the dependency set is small and the image stays legible.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so edits to source do not invalidate the install layer.
COPY pyproject.toml README.md ./
COPY acme_ap ./acme_ap
RUN pip install --no-cache-dir -e .

COPY main.py ./
COPY data ./data
COPY tests ./tests

# Runs as a non-root user; the database lives on a volume-friendly path.
RUN useradd --create-home --uid 10001 acme && mkdir -p /app/data-store/uploads && chown -R acme:acme /app
USER acme

ENV DATABASE_PATH=/app/data-store/acme.db \
    UPLOAD_DIR=/app/data-store/uploads
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"

CMD ["uvicorn", "acme_ap.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
