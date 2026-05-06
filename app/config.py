"""Configuración cargada de variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # GitHub
    github_token: str
    blog_repo: str  # ej. "leonvillamayor/blog"
    blog_repo_path: Path

    # Cloudflare (lectura, opcional para mostrar estado de builds)
    cloudflare_token: str | None
    cloudflare_account_id: str | None
    cloudflare_zone_id: str | None

    # Convenciones
    draft_branch_prefix: str = "drafts/"
    article_path_prefix: str = "content/posts/"
    infra_paths: tuple[str, ...] = field(
        default_factory=lambda: (
            "layouts/", "static/", "themes/", "hugo.toml",
            "i18n/", "data/", "archetypes/", "tools/",
            "schema/", "documentacion/",
        )
    )

    # Runtime
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    refresh_interval_seconds: int = 60


def load_settings() -> Settings:
    def _req(key: str) -> str:
        v = os.environ.get(key)
        if not v:
            raise RuntimeError(f"Falta variable obligatoria {key} en el entorno")
        return v

    infra_csv = os.environ.get(
        "INFRA_PATHS",
        "layouts/,static/,themes/,hugo.toml,i18n/,data/,archetypes/,tools/,schema/,documentacion/",
    )

    return Settings(
        github_token=_req("GITHUB_TOKEN"),
        blog_repo=os.environ.get("BLOG_REPO", "leonvillamayor/blog"),
        blog_repo_path=Path(os.environ.get("BLOG_REPO_PATH", "/opt/blog-pipeline/data/repo")),
        cloudflare_token=os.environ.get("CLOUDFLARE_TOKEN"),
        cloudflare_account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        cloudflare_zone_id=os.environ.get("CLOUDFLARE_ZONE_ID"),
        draft_branch_prefix=os.environ.get("DRAFT_BRANCH_PREFIX", "drafts/"),
        article_path_prefix=os.environ.get("ARTICLE_PATH_PREFIX", "content/posts/"),
        infra_paths=tuple(p.strip() for p in infra_csv.split(",") if p.strip()),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        refresh_interval_seconds=int(os.environ.get("REFRESH_INTERVAL_SECONDS", "60")),
    )
