.PHONY: install dev test lint fmt up down clean backup restore \
        deploy-check deploy version

# --- Local ------------------------------------------------------------

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

# --- Deploy -----------------------------------------------------------
#
# Deploy target = rsync the repo to pathseeker + rebuild the Docker
# image + restart the container + smoke-test /version. The rsync
# exclude list is CRITICAL — omitting --exclude=.env once wiped the
# production env file (recovered from the running container's env,
# but scary).
#
# Override the host with `make deploy DEPLOY_HOST=user@example.com`.

DEPLOY_HOST     ?= root@pathseeker
DEPLOY_PATH     ?= /srv/rssfriends
DEPLOY_COMPOSE  ?= docker-compose.pathseeker.yml
HEALTHCHECK_URL ?= https://rssfriends.com/version

RSYNC_EXCLUDES = \
  --exclude=.venv --exclude=__pycache__ --exclude=data --exclude=backups \
  --exclude=.git --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='*.egg-info' --exclude='$(DEPLOY_COMPOSE)' --exclude='.env'

version:
	@grep '^__version__' app/__init__.py | cut -d'"' -f2

# Dry-run: show what rsync WOULD send, without touching production.
deploy-check:
	@echo "→ dry-run rsync to $(DEPLOY_HOST):$(DEPLOY_PATH)"
	rsync -avzn --delete $(RSYNC_EXCLUDES) ./ $(DEPLOY_HOST):$(DEPLOY_PATH)/

# Full deploy: tests + lint + rsync + rebuild + smoke-test.
# Fails fast at each step so a broken commit never reaches prod.
deploy:
	@echo "→ Running tests + lint before deploy…"
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory lint
	@V=$$($(MAKE) --no-print-directory version); \
	  echo "→ Deploying v$$V to $(DEPLOY_HOST):$(DEPLOY_PATH)"; \
	  rsync -avz --delete $(RSYNC_EXCLUDES) ./ $(DEPLOY_HOST):$(DEPLOY_PATH)/ | tail -6; \
	  echo "→ Rebuilding container on $(DEPLOY_HOST)"; \
	  ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker-compose -f $(DEPLOY_COMPOSE) up -d --build' | tail -6; \
	  echo "→ Waiting for app to come up…"; \
	  sleep 4; \
	  LIVE=$$(curl -sf $(HEALTHCHECK_URL) || echo '{"version":"UNREACHABLE"}'); \
	  echo "→ /version says: $$LIVE"; \
	  echo "$$LIVE" | grep -q "\"$$V\"" \
	    && echo "✅ Deploy of v$$V verified." \
	    || (echo "❌ /version does not match v$$V — check container logs!" && exit 2)
