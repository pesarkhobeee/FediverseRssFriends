.PHONY: install dev test lint fmt up down clean backup restore \
        deploy-check deploy version require-deploy-config

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
# Deploy target = rsync the repo to your server + rebuild the Docker
# image + restart the container + smoke-test /version. The rsync
# exclude list is CRITICAL — omitting --exclude=.env once wiped the
# production env file. DEPLOY_COMPOSE is excluded too: if it names the
# wrong file, `--delete` removes the server's real compose file.
#
# Instance-specific values live in instance/deploy.mk (see
# instance/README.md) or on the command line:
#   make deploy DEPLOY_HOST=user@your-server \
#               HEALTHCHECK_URL=https://your-domain.example/version

-include instance/deploy.mk

DEPLOY_HOST     ?=
DEPLOY_PATH     ?= /srv/rssfriends
DEPLOY_COMPOSE  ?= docker-compose.prod.yml
HEALTHCHECK_URL ?=

RSYNC_EXCLUDES = \
  --exclude=.venv --exclude=__pycache__ --exclude=data --exclude=backups \
  --exclude=.git --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='*.egg-info' --exclude='$(DEPLOY_COMPOSE)' --exclude='.env'

version:
	@grep '^__version__' app/__init__.py | cut -d'"' -f2

# Dry-run: show what rsync WOULD send, without touching production.
deploy-check: require-deploy-config
	@echo "→ dry-run rsync to $(DEPLOY_HOST):$(DEPLOY_PATH)"
	rsync -avzn --delete $(RSYNC_EXCLUDES) ./ $(DEPLOY_HOST):$(DEPLOY_PATH)/

require-deploy-config:
	@if [ -z "$(DEPLOY_HOST)" ] || [ -z "$(HEALTHCHECK_URL)" ]; then \
	  echo "❌ DEPLOY_HOST and HEALTHCHECK_URL must be set (instance/deploy.mk or command line)."; \
	  exit 2; \
	fi

# Full deploy: tests + lint + rsync + rebuild + smoke-test.
# Fails fast at each step so a broken commit never reaches prod.
deploy: require-deploy-config
	@echo "→ Running tests + lint before deploy…"
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory lint
	@V=$$($(MAKE) --no-print-directory version); \
	  echo "→ Deploying v$$V to $(DEPLOY_HOST):$(DEPLOY_PATH)"; \
	  rsync -avz --delete $(RSYNC_EXCLUDES) ./ $(DEPLOY_HOST):$(DEPLOY_PATH)/ | tail -6; \
	  echo "→ Rebuilding container on $(DEPLOY_HOST)"; \
	  ssh $(DEPLOY_HOST) 'cd $(DEPLOY_PATH) && docker-compose -f $(DEPLOY_COMPOSE) up -d --build' | tail -6; \
	  echo "→ Waiting for app to come up…"; \
	  for i in $$(seq 1 15); do \
	    sleep 2; \
	    LIVE=$$(curl -sf $(HEALTHCHECK_URL) || echo '{"version":"UNREACHABLE"}'); \
	    echo "$$LIVE" | grep -q "\"$$V\"" && break; \
	  done; \
	  echo "→ /version says: $$LIVE"; \
	  echo "$$LIVE" | grep -q "\"$$V\"" \
	    && echo "✅ Deploy of v$$V verified." \
	    || (echo "❌ /version does not match v$$V — check container logs!" && exit 2)
