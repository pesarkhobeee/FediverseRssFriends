.PHONY: install dev test lint fmt up down

install:
	pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload

test:
	pytest -q

lint:
	ruff check .

fmt:
	ruff format .
	ruff check --fix .

up:
	docker compose up --build

down:
	docker compose down
