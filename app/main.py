import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, mail
from .auth import consume_token, current_user, issue_magic_link, peek_token
from .dashboard import router as dashboard_router
from .db import User, get_session, init_db
from .discover import router as discover_router
from .favorites import router as favorites_router
from .follow import router as follow_router
from .public import router as public_router
from .refresh import run_cycle
from .timeline import router as timeline_router

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


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

# Expose the version to every template (footer, admin, etc.).
templates.env.globals["app_version"] = __version__


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", ctx.get("user"))
    return templates.TemplateResponse(request, template, ctx)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse(f"/u/{user.handle}", status_code=303)
    return render(request, "index.html", user=None)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_session),
):
    try:
        _, url = issue_magic_link(db, email)
    except HTTPException as e:
        return render(request, "login_error.html", user=None, message=e.detail)
    mail.send_magic_link(email.strip().lower(), url)
    # Dev mode: no email delivery configured at all → surface the link
    # on the page so the developer can click through without email.
    is_dev = not config.RESEND_API_KEY and not config.SMTP_HOST
    dev_link = url if is_dev else None
    return render(request, "login_sent.html", user=None, email=email, dev_link=dev_link)


@app.get("/auth/verify", response_class=HTMLResponse)
def verify_confirm(request: Request, token: str, db: Session = Depends(get_session)):
    """Validate but DON'T consume the token — render a confirmation page
    with a button that POSTs back. Prevents Gmail's link scanner (and
    similar) from silently consuming the single-use token on delivery."""
    peek_token(db, token)  # 400 if invalid/expired
    return render(request, "verify_confirm.html", user=None, token=token)


@app.post("/auth/verify")
def verify(request: Request, token: str = Form(...), db: Session = Depends(get_session)):
    user = consume_token(db, token)
    request.session["user_id"] = user.id
    return RedirectResponse(f"/u/{user.handle}", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


app.include_router(dashboard_router)
app.include_router(public_router)
app.include_router(favorites_router)
app.include_router(follow_router)
app.include_router(timeline_router)
app.include_router(discover_router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/version")
def version():
    """Plain-text version endpoint for uptime checkers + deploy scripts."""
    return {"version": __version__}
