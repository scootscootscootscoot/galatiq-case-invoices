.PHONY: help install db run web test eval lint fmt format-check typecheck check clean docker demo

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package and dev dependencies
	python -m venv .venv 2>/dev/null || true
	.venv/bin/pip install -e ".[dev]"

db:  ## Create and seed the database
	.venv/bin/python -m acme_ap.db.seed

run:  ## Process one invoice (INVOICE=path)
	.venv/bin/python main.py --invoice_path=$(or $(INVOICE),data/invoices/invoice_1001.txt)

web:  ## Serve the dashboard on :8000
	.venv/bin/uvicorn acme_ap.api.app:app --reload --port 8000

test:  ## Run the test suite
	.venv/bin/python -m pytest -q

eval:  ## Score the pipeline against the golden set
	.venv/bin/python -m acme_ap.eval

lint:  ## Lint
	.venv/bin/ruff check .

fmt:  ## Format
	.venv/bin/ruff format .

typecheck:  ## Type-check
	.venv/bin/mypy acme_ap

format-check:  ## Verify formatting without changing files
	.venv/bin/ruff format --check .

check: lint format-check typecheck test eval  ## Local code checks (CI also builds Docker)

demo:  ## Record the browser workflow (requires demo extras, Chromium, ffmpeg, Tesseract)
	.venv/bin/python scripts/record_demo.py

docker:  ## Build and run the container
	docker compose up --build

clean:  ## Remove caches and the local database
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ acme.db acme.db-wal acme.db-shm
