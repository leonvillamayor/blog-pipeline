"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import gitops, state
from .config import load_settings

logger = logging.getLogger("blog-pipeline")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings

    # Configurar credential helper antes del clone (evita PAT en argv)
    gitops.configure_credential_store(settings.github_token)

    # Asegurar clone del repo blog (URL HTTPS limpia; auth via credential.helper)
    repo_url = f"https://github.com/{settings.blog_repo}.git"
    try:
        gitops.ensure_clone(settings.blog_repo_path, repo_url)
        gitops.fetch_all(settings.blog_repo_path)
    except Exception as exc:
        logger.error("Init repo falló: %s", exc)
        raise

    # Background task de refresh periódico
    refresh_task = asyncio.create_task(_periodic_refresh(app, settings))
    yield
    refresh_task.cancel()


async def _periodic_refresh(app: FastAPI, settings) -> None:
    while True:
        try:
            await asyncio.to_thread(gitops.fetch_all, settings.blog_repo_path)
            app.state.pipeline = await asyncio.to_thread(state.build_state, settings)
            logger.info(
                "Refresh OK — pending=%d dev=%d preprod=%d prod=%d",
                len(app.state.pipeline.pending),
                len(app.state.pipeline.in_dev),
                len(app.state.pipeline.in_preprod),
                len(app.state.pipeline.in_prod),
            )
        except Exception as exc:
            logger.error("Refresh falló: %s", exc)
        await asyncio.sleep(settings.refresh_interval_seconds)


app = FastAPI(title="blog-pipeline", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    pipeline = getattr(app.state, "pipeline", None)
    if pipeline is None:
        # primer render antes del primer refresh: build sincrono
        pipeline = state.build_state(app.state.settings)
        app.state.pipeline = pipeline
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "p": pipeline, "settings": app.state.settings},
    )


@app.get("/api/state")
async def api_state():
    """JSON con el estado actual del pipeline (para integraciones / debug)."""
    pipeline = getattr(app.state, "pipeline", None)
    if pipeline is None:
        pipeline = state.build_state(app.state.settings)

    def _ser(arts):
        return [
            {
                "slug": a.slug,
                "title": a.title,
                "is_draft": a.is_draft,
                "pending_branch": a.pending_branch,
            }
            for a in arts
        ]

    return {
        "last_refreshed_iso": pipeline.last_refreshed_iso,
        "pending": _ser(pipeline.pending),
        "in_dev": _ser(pipeline.in_dev),
        "in_preprod": _ser(pipeline.in_preprod),
        "in_prod": _ser(pipeline.in_prod),
    }
