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

from . import config, mail
from .auth import consume_token, current_user, issue_magic_link
from .dashboard import router as dashboard_router
from .db import User, get_session, init_db
from .favorites import router as favorites_router
from .public import router as public_router
from .refresh import run_cycle

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


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", ctx.get("user"))
    return templates.TemplateResponse(request, template, ctx)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse("/dashboard", status_code=303)
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
    dev_link = url if not config.SMTP_HOST else None
    return render(request, "login_sent.html", user=None, email=email, dev_link=dev_link)


@app.get("/auth/verify")
def verify(request: Request, token: str, db: Session = Depends(get_session)):
    user = consume_token(db, token)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


app.include_router(dashboard_router)
app.include_router(public_router)
app.include_router(favorites_router)


@app.get("/healthz")
def healthz():
    return {"ok": True}
