.PHONY: test lint format typecheck check install dev clean

test:
	python3 -m pytest tests/ -q

lint:
	python3 -m ruff check .

format:
	python3 -m ruff format .

typecheck:
	python3 -m mypy infra_gen/

check: lint typecheck test

install:
	pip install .

dev:
	pip install -e ".[dev]"

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
