.PHONY: install dev test lint fmt up down clean backup restore

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

backup:
	./scripts/backup.sh

restore:
	@[ -n "$(FROM)" ] || (echo "usage: make restore FROM=backups/rssfriends-...sqlite.gz" && exit 2)
	./scripts/restore.sh "$(FROM)"
