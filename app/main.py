"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audit, gitops, promote, state
from .config import load_settings
from .github_client import GitHubClient, GitHubError

logger = logging.getLogger("blog-pipeline")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings

    # Configurar credential helper antes del clone (evita PAT en argv).
    # El fichero vive en data/ porque la home del usuario está read-only
    # bajo systemd hardening (ProtectSystem=strict).
    cred_file = settings.blog_repo_path.parent / ".git-credentials"
    gitops.configure_credential_store(settings.github_token, cred_file)

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


def _board_context(pipeline) -> dict:
    """Construye el contexto para board.html (también usado por audit_log inclusion)."""
    pr_for_slug = {}
    for pr in pipeline.gui_prs:
        if pr.slug:
            pr_for_slug[pr.slug] = pr
    events = audit.read_recent(_audit_path(), limit=15)
    return {
        "p": pipeline,
        "settings": app.state.settings,
        "events": events,
        "gui_prs": pipeline.gui_prs,
        "pr_for_slug": pr_for_slug,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    pipeline = getattr(app.state, "pipeline", None)
    if pipeline is None:
        pipeline = state.build_state(app.state.settings)
        app.state.pipeline = pipeline
    return templates.TemplateResponse(
        request, "dashboard.html", _board_context(pipeline),
    )


@app.get("/board", response_class=HTMLResponse)
async def board_partial(request: Request):
    """Partial: solo el contenido dinámico (sin layout/scripts).

    Usado por el botón refresh y el auto-refresh tras acciones (HX-Trigger).
    Forza un refresh de pipeline state primero para que llegue al instante.
    """
    pipeline = await asyncio.to_thread(state.build_state, app.state.settings)
    app.state.pipeline = pipeline
    return templates.TemplateResponse(
        request, "partials/board.html", _board_context(pipeline),
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


# ============================================================
# Fase 2 — Endpoints de acción (POST)
# ============================================================
#
# Cada endpoint devuelve un fragmento HTML para HTMX swap. Los errores
# se renderizan como banner de error en línea, no como JSON 500.

def _gh_client() -> GitHubClient:
    s = app.state.settings
    return GitHubClient(token=s.github_token, repo=s.blog_repo)


def _audit_path() -> Path:
    s = app.state.settings
    return s.blog_repo_path.parent.parent / "data" / "audit.log"


def _user_from_request(request: Request) -> str:
    """CF Access inyecta este header tras OTP."""
    return request.headers.get("Cf-Access-Authenticated-User-Email", "unknown")


def _action_response(request: Request, kind: str, message: str, pr_url: str | None = None):
    """Devuelve banner de resultado. Si la acción tuvo éxito, dispara
    `board-refresh` event en el cliente vía HX-Trigger header — que el JS
    escucha para hacer un refresh diferido del board partial.
    """
    headers = {}
    if kind == "success":
        headers["HX-Trigger"] = "board-refresh"
    response = templates.TemplateResponse(
        request,
        "partials/action_result.html",
        {"kind": kind, "message": message, "pr_url": pr_url},
        headers=headers,
    )
    return response


@app.post("/api/deploy/{slug}/dev", response_class=HTMLResponse)
async def deploy_to_dev(request: Request, slug: str):
    """Mergea drafts/<slug> → dev (vía PR + auto-merge)."""
    user = _user_from_request(request)
    pipeline = getattr(app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(503, "Pipeline state no inicializado todavía")

    art = next((a for a in pipeline.pending if a.slug == slug), None)
    if not art or not art.pending_branch:
        audit.log_event(_audit_path(), user=user, action="deploy", level="dev",
                         slug=slug, ok=False, reason="not_in_pending")
        return _action_response(request, "error", f"Slug '{slug}' no en pending o sin branch.")

    try:
        gh = _gh_client()
        result = await asyncio.to_thread(
            promote.deploy_pending_to_dev, gh, slug, art.pending_branch,
        )
    except (GitHubError, ValueError, RuntimeError) as exc:
        logger.error("deploy_to_dev falló: %s", exc)
        audit.log_event(_audit_path(), user=user, action="deploy", level="dev",
                         slug=slug, ok=False, reason=str(exc))
        return _action_response(request, "error", str(exc))

    audit.log_event(_audit_path(), user=user, action="deploy", level="dev",
                     slug=slug, ok=True, pr=result.pr_number, pr_url=result.pr_url)
    return _action_response(
        request, "success",
        f"PR #{result.pr_number} creado. Auto-merge: {'enabled' if result.auto_merge_enabled else 'disabled'}.",
        pr_url=result.pr_url,
    )


@app.post("/api/deploy/{slug}/preprod", response_class=HTMLResponse)
async def deploy_to_preprod(request: Request, slug: str):
    """Promociona artículo de dev a preprod."""
    user = _user_from_request(request)
    try:
        gh = _gh_client()
        result = await asyncio.to_thread(
            promote.promote_article, gh, slug, "dev", "preprod",
        )
    except (GitHubError, ValueError, RuntimeError) as exc:
        logger.error("deploy_to_preprod falló: %s", exc)
        audit.log_event(_audit_path(), user=user, action="deploy", level="preprod",
                         slug=slug, ok=False, reason=str(exc))
        return _action_response(request, "error", str(exc))
    audit.log_event(_audit_path(), user=user, action="deploy", level="preprod",
                     slug=slug, ok=True, pr=result.pr_number, pr_url=result.pr_url,
                     files=result.files_committed)
    return _action_response(
        request, "success",
        f"PR #{result.pr_number} creado ({result.files_committed} ficheros). "
        f"Auto-merge: {'enabled' if result.auto_merge_enabled else 'disabled'}.",
        pr_url=result.pr_url,
    )


@app.post("/api/deploy/{slug}/prod", response_class=HTMLResponse)
async def deploy_to_prod(request: Request, slug: str):
    """Promociona artículo de preprod a main."""
    user = _user_from_request(request)
    try:
        gh = _gh_client()
        result = await asyncio.to_thread(
            promote.promote_article, gh, slug, "preprod", "main",
        )
    except (GitHubError, ValueError, RuntimeError) as exc:
        logger.error("deploy_to_prod falló: %s", exc)
        audit.log_event(_audit_path(), user=user, action="deploy", level="prod",
                         slug=slug, ok=False, reason=str(exc))
        return _action_response(request, "error", str(exc))
    audit.log_event(_audit_path(), user=user, action="deploy", level="prod",
                     slug=slug, ok=True, pr=result.pr_number, pr_url=result.pr_url,
                     files=result.files_committed)
    return _action_response(
        request, "success",
        f"PR #{result.pr_number} creado ({result.files_committed} ficheros). "
        f"Auto-merge: {'enabled' if result.auto_merge_enabled else 'disabled'}.",
        pr_url=result.pr_url,
    )


@app.post("/api/delete/{slug}/{level}", response_class=HTMLResponse)
async def delete_from(request: Request, slug: str, level: str):
    """Borra el artículo de `level` (dev | preprod | main | pending)."""
    user = _user_from_request(request)
    if level == "pending":
        pipeline = getattr(app.state, "pipeline", None)
        if pipeline is None:
            raise HTTPException(503, "state no inicializado")
        art = next((a for a in pipeline.pending if a.slug == slug), None)
        if not art or not art.pending_branch:
            audit.log_event(_audit_path(), user=user, action="delete", level="pending",
                             slug=slug, ok=False, reason="not_in_pending")
            return _action_response(request, "error", f"Slug '{slug}' no en pending.")
        try:
            gh = _gh_client()
            await asyncio.to_thread(gh.delete_branch, art.pending_branch)
        except GitHubError as exc:
            audit.log_event(_audit_path(), user=user, action="delete", level="pending",
                             slug=slug, ok=False, reason=str(exc))
            return _action_response(request, "error", f"No se pudo borrar branch: {exc}")
        audit.log_event(_audit_path(), user=user, action="delete", level="pending",
                         slug=slug, ok=True, branch=art.pending_branch)
        return _action_response(
            request, "success",
            f"Feature branch `{art.pending_branch}` borrada.",
        )

    if level not in {"dev", "preprod", "main"}:
        raise HTTPException(400, f"Level inválido: {level}")

    try:
        gh = _gh_client()
        result = await asyncio.to_thread(promote.delete_article, gh, slug, level)
    except (GitHubError, ValueError, RuntimeError) as exc:
        logger.error("delete falló: %s", exc)
        audit.log_event(_audit_path(), user=user, action="delete", level=level,
                         slug=slug, ok=False, reason=str(exc))
        return _action_response(request, "error", str(exc))
    audit.log_event(_audit_path(), user=user, action="delete", level=level,
                     slug=slug, ok=True, pr=result.pr_number, pr_url=result.pr_url,
                     files=result.files_committed)
    return _action_response(
        request, "success",
        f"PR #{result.pr_number} de borrado creado ({result.files_committed} ficheros). "
        f"Auto-merge: {'enabled' if result.auto_merge_enabled else 'disabled'}.",
        pr_url=result.pr_url,
    )


@app.get("/api/audit", response_class=HTMLResponse)
async def api_audit(request: Request):
    """Renderiza partial con los últimos eventos del audit log."""
    events = audit.read_recent(_audit_path(), limit=15)
    return templates.TemplateResponse(
        request, "partials/audit_log.html", {"events": events},
    )
