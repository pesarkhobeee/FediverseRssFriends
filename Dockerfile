FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY instance ./instance
RUN uv sync --frozen --no-dev

ENV RSSFRIENDS_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips: trust X-Forwarded-Proto from
# the reverse proxy so request.url.scheme is https in prod. og:url and
# og:image (which social platforms follow) then use https, matching
# what browsers actually see.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
