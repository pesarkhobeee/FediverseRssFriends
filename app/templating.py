"""Shared Jinja2Templates factory. Every router MUST get its templates
through this so `app_version` (used for the ?v= cache-buster on
static assets) and shared filters/globals are set uniformly.

Historically each router built its own Jinja2Templates and only main.py
set the `app_version` global — so every page rendered outside main
served `?v=` with an empty value and browsers cached CSS forever."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import __version__
from .utils import relative_time

APP_DIR = Path(__file__).resolve().parent


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=APP_DIR / "templates")
    t.env.filters.setdefault("relative_time", relative_time)
    t.env.globals["app_version"] = __version__
    return t
