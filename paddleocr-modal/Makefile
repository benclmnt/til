.PHONY: help sync deploy run-url

help:
	@echo "Targets:"
	@echo "  make sync     - uv sync"
	@echo "  make deploy   - sync + modal deploy (GPU PaddleOCR API)"
	@echo "  make run-url  - sync + modal run (print HTTPS URL, cold-starts a replica)"

sync:
	uv sync

deploy: sync
	uv run modal deploy deploy_paddleocr_modal.py

run-url: sync
	uv run modal run deploy_paddleocr_modal.py
