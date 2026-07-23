import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, mail
from .auth import consume_token, current_user, issue_magic_link, peek_token
from .bookmarks import router as bookmarks_router
from .dashboard import router as dashboard_router
from .db import User, get_session, init_db
from .discover import router as discover_router
from .feed_page import router as feed_page_router
from .follow import router as follow_router
from .personal_feeds import router as personal_feeds_router
from .public import router as public_router
from .refresh import run_cycle
from .templating import make_templates
from .timeline import router as timeline_router

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = make_templates()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler: BackgroundScheduler | None = None
    if config.ENABLE_SCHEDULER:
        from datetime import datetime, timedelta

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            run_cycle,
            trigger=IntervalTrigger(minutes=config.REFRESH_INTERVAL_MINUTES),
            id="feed-refresh",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now() + timedelta(seconds=15),  # dev-friendly first tick
        )
        scheduler.start()
        log.info("scheduler started, interval=%dmin", config.REFRESH_INTERVAL_MINUTES)
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, version=__version__)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", ctx.get("user"))
    return templates.TemplateResponse(request, template, ctx)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: User | None = Depends(current_user),
    deleted: int = 0,
):
    if user:
        return RedirectResponse(f"/u/{user.handle}", status_code=303)
    return render(request, "index.html", user=None, deleted=bool(deleted))


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_session),
):
    # If the landing hero's inline signup posted via HTMX, return a
    # partial that swaps just the signup card — no full-page bounce.
    hx = request.headers.get("HX-Request") == "true"
    try:
        _, url = issue_magic_link(db, email)
    except HTTPException as e:
        template = "_landing_signup_error.html" if hx else "login_error.html"
        return render(request, template, user=None, message=e.detail, email=email)
    mail.send_magic_link(email.strip().lower(), url)
    # Dev mode: no email delivery configured at all → surface the link
    # on the page so the developer can click through without email.
    is_dev = not config.RESEND_API_KEY and not config.SMTP_HOST
    dev_link = url if is_dev else None
    template = "_landing_signup_sent.html" if hx else "login_sent.html"
    return render(request, template, user=None, email=email, dev_link=dev_link)


@app.get("/auth/verify", response_class=HTMLResponse)
def verify_confirm(request: Request, token: str, db: Session = Depends(get_session)):
    """Validate but DON'T consume the token — render a confirmation page
    with a button that POSTs back. Prevents Gmail's link scanner (and
    similar) from silently consuming the single-use token on delivery."""
    try:
        peek_token(db, token)
    except HTTPException:
        return render(request, "verify_error.html", user=None)
    return render(request, "verify_confirm.html", user=None, token=token)


@app.post("/auth/verify")
def verify(request: Request, token: str = Form(...), db: Session = Depends(get_session)):
    try:
        user = consume_token(db, token)
    except HTTPException:
        return render(request, "verify_error.html", user=None)
    request.session["user_id"] = user.id
    return RedirectResponse(f"/u/{user.handle}", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


app.include_router(dashboard_router)
app.include_router(public_router)
app.include_router(bookmarks_router)
app.include_router(follow_router)
app.include_router(timeline_router)
app.include_router(discover_router)
app.include_router(feed_page_router)
app.include_router(personal_feeds_router)


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request, user: User | None = Depends(current_user)):
    return render(request, "help.html", user=user)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/version")
def version():
    """Plain-text version endpoint for uptime checkers + deploy scripts."""
    return {"version": __version__}


# --- PWA service worker -----------------------------------------------
#
# The SW file must be served from the ROOT origin path (`/sw.js`), not
# `/static/sw.js`, because a service worker's scope is limited to the
# directory of its own URL. Registering `/static/sw.js` would only
# control `/static/*` — useless. So we read the file from disk once
# and swap `__VERSION__` with the current app version so bumps
# invalidate the SW's static cache automatically.

_SW_PATH = APP_DIR / "static" / "sw.js"
_SW_BODY: str | None = None


def _sw_body() -> str:
    global _SW_BODY
    if _SW_BODY is None:
        _SW_BODY = _SW_PATH.read_text().replace("__VERSION__", __version__)
    return _SW_BODY


@app.get("/manifest.webmanifest")
def pwa_manifest() -> JSONResponse:
    """Serve the PWA manifest dynamically so we can version-tag every
    icon URL. Without this, browsers + CDNs cache /static/icons/*.png
    forever — because their URLs don't change across releases, only
    their contents do."""
    v = __version__
    return JSONResponse(
        {
            "name": "rssFriends",
            "short_name": "rssFriends",
            "description": (
                "A social RSS reader — follow curators, share what you "
                "bookmark, cross-post from Mastodon and YouTube."
            ),
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#f26522",
            "icons": [
                {
                    "src": f"/static/icons/icon-192.png?v={v}",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": f"/static/icons/icon-512.png?v={v}",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": f"/static/icons/icon-maskable-512.png?v={v}",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
def service_worker() -> PlainTextResponse:
    return PlainTextResponse(
        _sw_body(),
        media_type="application/javascript",
        headers={
            # Browsers already bypass HTTP cache when re-fetching an SW,
            # but be explicit.
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )
