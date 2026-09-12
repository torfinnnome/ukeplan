import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from ukeplan.config import settings
from ukeplan.db import init_db, engine, purge_stale_items
from ukeplan.models import FamilyMember
from sqlmodel import Session, select
from ukeplan.api import api_router
from ukeplan.shortcut import generate_shortcut_file, shortcut_filename
from ukeplan.email_poller import start_imap_poller
import uvicorn
import pathlib
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ukeplan")

templates_dir = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


async def _purge_stale_items_loop():
    """Background task: remove old calendar entries and uploaded documents.
    Removes plan items older than the retention window and uploaded documents
    (PDFs/photos) created before it, plus their files on disk. Runs once at
    startup, then on a fixed interval — no external cron required."""
    while True:
        try:
            removed = await asyncio.to_thread(purge_stale_items)
            if removed:
                logger.info(f"Auto-purge: removed {removed} stale record(s) older than {settings.item_retention_weeks} weeks (calendar items + uploaded documents)")
        except Exception:
            logger.exception("Item auto-purge failed; will retry next interval")
        await asyncio.sleep(settings.item_purge_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database tables
    init_db()
    # Startup: background tasks
    poller_task = None
    if settings.imap_enabled:
        poller_task = asyncio.create_task(start_imap_poller())
    purge_task = None
    if settings.item_autopurge:
        purge_task = asyncio.create_task(_purge_stale_items_loop())
    yield
    # Shutdown: clean up background tasks
    for task in (poller_task, purge_task):
        if task and not task.done():
            task.cancel()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

# Mount REST API
app.include_router(api_router)

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    with Session(engine) as session:
        members = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
        family_members = [{"id": m.id, "name": m.name, "school_class": m.school_class, "display_order": m.display_order} for m in members]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "family_members": family_members,
            "app_name": settings.app_name,
            "ai_model": settings.ai_model,
        },
    )


@app.get("/setup/apple-shortcut", response_class=HTMLResponse)
def shortcut_setup_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="shortcut_setup.html",
        context={
            "app_name": settings.app_name,
        },
    )


@app.get("/setup/apple-shortcut/download", include_in_schema=False)
def apple_shortcut_download(request: Request, name: str | None = None):
    origin = str(request.base_url).rstrip("/")
    clean = (name or "").strip()[:64] or None
    data, is_signed = generate_shortcut_file(origin + "/api/ingest", name=clean)
    filename = shortcut_filename(clean or "Del til Ukeplan", is_signed)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request):
    with Session(engine) as session:
        members = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
        family_members = [{"id": m.id, "name": m.name, "school_class": m.school_class, "display_order": m.display_order} for m in members]
    return templates.TemplateResponse(
        request=request,
        name="documents.html",
        context={
            "family_members": family_members,
            "app_name": settings.app_name,
        },
    )



@app.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    with Session(engine) as session:
        members = session.exec(select(FamilyMember).order_by(FamilyMember.display_order, FamilyMember.id)).all()
        family_members = [{"id": m.id, "name": m.name, "school_class": m.school_class, "display_order": m.display_order} for m in members]
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            "family_members": family_members,
            "app_name": settings.app_name,
        },
    )


def main():
    uvicorn.run(
        "ukeplan:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
