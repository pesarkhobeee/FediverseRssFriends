.PHONY: install dev test lint fmt up down clean

install:
	uv sync --all-extras

dev:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

up:
	docker compose up --build

down:
	docker compose down

clean:
	rm -rf .venv .pytest_cache .ruff_cache dist build *.egg-info
