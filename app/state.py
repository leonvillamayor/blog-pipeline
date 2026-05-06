"""Modelo del estado del pipeline: qué artículo está en qué columna.

Lee el repo clonado (gestionado por gitops.fetch_all) y construye:

   pending  : feature branches drafts/<slug>
   dev      : artículos en origin/dev (con su flag draft)
   preprod  : artículos en origin/preprod
   prod     : artículos en origin/main

La GUI consume esta estructura.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .config import Settings

_DRAFT_RE = re.compile(r"^draft:\s*(true|false)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE)


@dataclass
class Article:
    slug: str
    title: str = ""
    is_draft: bool = False
    branches_with_article: set[str] = field(default_factory=set)
    pending_branch: str | None = None  # nombre de feature branch si aplica


@dataclass
class PipelineState:
    pending: list[Article] = field(default_factory=list)
    in_dev: list[Article] = field(default_factory=list)
    in_preprod: list[Article] = field(default_factory=list)
    in_prod: list[Article] = field(default_factory=list)
    last_refreshed_iso: str = ""


def _read_article_meta(repo_path: Path, branch: str, slug: str, article_prefix: str) -> tuple[str, bool]:
    """Devuelve (title, is_draft) leyendo el frontmatter ES del artículo en esa rama."""
    candidates = [
        f"{article_prefix}{slug}/index.md",  # bundle nuevo
        f"{article_prefix}{slug}/_index.md",  # section
    ]
    for path in candidates:
        text = gitops.read_file_at(repo_path, branch, path)
        if text:
            title = ""
            m = _TITLE_RE.search(text)
            if m:
                title = m.group(1).strip().strip('"').strip("'")
            is_draft = False
            md = _DRAFT_RE.search(text)
            if md:
                is_draft = md.group(1) == "true"
            return title, is_draft

    # Fallback: buscar cualquier .md en el directorio del slug (legacy NNNN/<num>_*.md)
    # Esto requiere ls-tree del directorio en la rama.
    out = gitops._run(
        ["ls-tree", "-r", "--name-only", f"origin/{branch}", "--", f"{article_prefix}{slug}/"],
        cwd=repo_path,
        check=False,
    )
    for line in out.splitlines():
        line = line.strip()
        if line.endswith(".md") and not line.endswith(".en.md"):
            text = gitops.read_file_at(repo_path, branch, line)
            if text:
                title = ""
                m = _TITLE_RE.search(text)
                if m:
                    title = m.group(1).strip().strip('"').strip("'")
                is_draft = False
                md = _DRAFT_RE.search(text)
                if md:
                    is_draft = md.group(1) == "true"
                return title, is_draft

    return slug, False


def build_state(settings: Settings) -> PipelineState:
    repo = settings.blog_repo_path
    prefix = settings.article_path_prefix
    draft_prefix = settings.draft_branch_prefix

    # Pending: feature branches drafts/*
    feature_branches = gitops.list_branches(repo, pattern=draft_prefix)
    pending: list[Article] = []
    for fb in feature_branches:
        slug = fb.removeprefix(draft_prefix)
        if not slug:
            continue
        # Algunos feature branches pueden no tener el artículo todavía;
        # buscamos en su tree.
        articles = gitops.list_articles_in_branch(repo, fb, prefix)
        # Si la rama tiene exactamente UN artículo nuevo, ese es el slug
        # candidato. Si no, usamos el sufijo de la rama.
        target_slug = articles[0] if len(articles) == 1 else slug
        title, is_draft = _read_article_meta(repo, fb, target_slug, prefix)
        pending.append(Article(
            slug=target_slug,
            title=title or target_slug,
            is_draft=is_draft,
            pending_branch=fb,
        ))

    # En cada rama principal: lista de artículos
    def articles_in(branch: str) -> list[Article]:
        slugs = gitops.list_articles_in_branch(repo, branch, prefix)
        result = []
        for s in slugs:
            title, is_draft = _read_article_meta(repo, branch, s, prefix)
            result.append(Article(slug=s, title=title or s, is_draft=is_draft))
        return result

    in_dev = articles_in("dev")
    in_preprod = articles_in("preprod")
    in_prod = articles_in("main")

    # Refinar columnas: un artículo aparece en TODAS las ramas hasta que se
    # promociona; lo natural es mostrarlo en la columna MÁS AVANZADA donde
    # esté presente.
    prod_slugs = {a.slug for a in in_prod}
    preprod_slugs = {a.slug for a in in_preprod}
    dev_slugs = {a.slug for a in in_dev}

    in_dev_only = [a for a in in_dev if a.slug not in preprod_slugs]
    in_preprod_only = [a for a in in_preprod if a.slug not in prod_slugs]

    from datetime import datetime, timezone
    return PipelineState(
        pending=pending,
        in_dev=in_dev_only,
        in_preprod=in_preprod_only,
        in_prod=in_prod,
        last_refreshed_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
