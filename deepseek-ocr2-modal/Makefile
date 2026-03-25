.PHONY: help sync deploy run-url

help:
	@echo "Targets:"
	@echo "  make sync     - uv sync"
	@echo "  make deploy   - sync + modal deploy (DeepSeek-OCR-2 vLLM API)"
	@echo "  make run-url  - sync + modal run (print HTTPS URL)"

sync:
	uv sync

deploy: sync
	uv run modal deploy deploy_deepseek_ocr2_modal.py

run-url: sync
	uv run modal run deploy_deepseek_ocr2_modal.py
