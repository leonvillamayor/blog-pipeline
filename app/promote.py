"""Operaciones de promoción y borrado artículo-level.

Usa GitHub Contents API para todas las escrituras: cada PUT/DELETE
produce un commit firmado por la web-flow GPG key de GitHub, lo que
satisface el requisito de branch protection 'Commits must have
verified signatures'.

Convención de paths que componen un artículo:
- content/posts/<slug>/   (todo el árbol — index.md, .en.md, images/)
- static/images/posts/<slug>.png  (cover, opcional, mismo nombre que slug)
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Literal

from .github_client import GitHubClient, GitHubError

Level = Literal["dev", "preprod", "main"]


@dataclass
class PromoteResult:
    pr_number: int
    pr_url: str
    work_branch: str
    files_committed: int
    auto_merge_enabled: bool


def _list_article_files(gh: GitHubClient, slug: str, branch: str) -> list[dict]:
    """Lista blobs del slug en `branch`. Cubre content/posts/<slug>/* y cover."""
    branch_sha = gh.get_branch_sha(branch)
    tree = gh.get_tree(branch_sha, recursive=True)

    article_prefix = f"content/posts/{slug}/"
    cover_path = f"static/images/posts/{slug}.png"

    files = [
        item for item in tree
        if item["type"] == "blob"
        and (item["path"].startswith(article_prefix) or item["path"] == cover_path)
    ]
    return files


def promote_article(
    gh: GitHubClient,
    slug: str,
    from_branch: str,
    to_branch: str,
) -> PromoteResult:
    """Promociona un artículo de `from_branch` a `to_branch`.

    Pasos:
    1. Listar ficheros del slug en `from_branch`.
    2. Crear branch worker `auto/promote-<slug>-to-<to_branch>-<ts>` desde `to_branch`.
    3. Por cada fichero del artículo, PUT /contents en el worker (commits firmados).
    4. Crear PR worker → to_branch.
    5. Habilitar auto-merge (merge_method=MERGE).
    """
    files = _list_article_files(gh, slug, from_branch)
    if not files:
        raise ValueError(f"No hay ficheros para slug='{slug}' en branch '{from_branch}'")

    ts = int(time.time())
    work = f"auto/promote-{slug}-to-{to_branch}-{ts}"
    base_sha = gh.get_branch_sha(to_branch)
    gh.create_branch(work, from_sha=base_sha)

    # Para cada fichero: leer blob de from_branch, escribir en worker.
    files_committed = 0
    for f in files:
        path = f["path"]
        # Leer contenido de from_branch
        src = gh.get_contents(path, ref=from_branch)
        if src is None:
            continue
        content_b64 = src["content"]  # ya base64 desde GitHub
        # Si el fichero ya existe en to_branch con sha distinto, lo pasamos
        # para update; si no existe, sha=None.
        existing = gh.get_contents(path, ref=work)
        sha_for_update = existing["sha"] if existing else None
        gh.put_contents(
            path=path,
            branch=work,
            content_b64=content_b64,
            message=f"promote({slug}): {path}",
            sha=sha_for_update,
        )
        files_committed += 1

    if files_committed == 0:
        gh.delete_branch(work)
        raise RuntimeError(f"No se commiteó ningún fichero para slug='{slug}'")

    # Crear PR
    title = f"🚀 promote({slug}): {from_branch} → {to_branch}"
    body = (
        f"Promoción artículo-level del slug `{slug}`.\n\n"
        f"- Origen: `{from_branch}`\n"
        f"- Destino: `{to_branch}`\n"
        f"- Ficheros: {files_committed}\n"
        f"- Worker branch: `{work}`\n\n"
        "Generado automáticamente por blog-pipeline GUI. Se mergea con "
        "`MERGE` (no squash, per memory `feedback_promotion_merge`)."
    )
    pr = gh.create_pr(head=work, base=to_branch, title=title, body=body)

    # Auto-merge
    auto_ok = True
    try:
        gh.enable_auto_merge(pr["node_id"], merge_method="MERGE")
    except GitHubError:
        auto_ok = False

    return PromoteResult(
        pr_number=pr["number"],
        pr_url=pr["html_url"],
        work_branch=work,
        files_committed=files_committed,
        auto_merge_enabled=auto_ok,
    )


def delete_article(
    gh: GitHubClient,
    slug: str,
    from_branch: str,
) -> PromoteResult:
    """Borra el artículo de `from_branch` vía PR.

    Pasos:
    1. Listar ficheros del slug en `from_branch`.
    2. Crear branch worker `auto/delete-<slug>-from-<from_branch>-<ts>`.
    3. Por cada fichero, DELETE /contents (commits firmados).
    4. Crear PR worker → from_branch.
    5. Habilitar auto-merge.
    """
    files = _list_article_files(gh, slug, from_branch)
    if not files:
        raise ValueError(f"No hay ficheros para slug='{slug}' en branch '{from_branch}'")

    ts = int(time.time())
    work = f"auto/delete-{slug}-from-{from_branch}-{ts}"
    base_sha = gh.get_branch_sha(from_branch)
    gh.create_branch(work, from_sha=base_sha)

    files_deleted = 0
    for f in files:
        # En el worker (recién creado a partir de from_branch) los ficheros
        # tienen el mismo SHA que en from_branch.
        gh.delete_contents(
            path=f["path"],
            branch=work,
            sha=f["sha"],
            message=f"delete({slug}): {f['path']}",
        )
        files_deleted += 1

    if files_deleted == 0:
        gh.delete_branch(work)
        raise RuntimeError(f"No se borró ningún fichero para slug='{slug}'")

    title = f"🗑️ delete({slug}) from {from_branch}"
    body = (
        f"Borrado artículo-level del slug `{slug}` de `{from_branch}`.\n\n"
        f"- Ficheros borrados: {files_deleted}\n"
        f"- Worker branch: `{work}`\n\n"
        "Generado automáticamente por blog-pipeline GUI."
    )
    pr = gh.create_pr(head=work, base=from_branch, title=title, body=body)

    auto_ok = True
    try:
        gh.enable_auto_merge(pr["node_id"], merge_method="MERGE")
    except GitHubError:
        auto_ok = False

    return PromoteResult(
        pr_number=pr["number"],
        pr_url=pr["html_url"],
        work_branch=work,
        files_committed=files_deleted,
        auto_merge_enabled=auto_ok,
    )


def deploy_pending_to_dev(
    gh: GitHubClient,
    slug: str,
    feature_branch: str,
) -> PromoteResult:
    """Mergea una feature branch `drafts/<slug>` a `dev` via PR.

    A diferencia de promote_article, aquí ya existe una branch con el
    artículo: solo hay que abrir PR feature → dev y auto-merge. No
    necesitamos copiar ficheros.
    """
    title = f"🚀 deploy({slug}): {feature_branch} → dev"
    body = (
        f"Despliegue del slug `{slug}` desde `{feature_branch}` a `dev`.\n\n"
        "Generado automáticamente por blog-pipeline GUI. Merge=MERGE."
    )
    pr = gh.create_pr(head=feature_branch, base="dev", title=title, body=body)

    auto_ok = True
    try:
        gh.enable_auto_merge(pr["node_id"], merge_method="MERGE")
    except GitHubError:
        auto_ok = False

    return PromoteResult(
        pr_number=pr["number"],
        pr_url=pr["html_url"],
        work_branch=feature_branch,
        files_committed=0,  # No commits creados por la GUI; ya estaban en la feature branch
        auto_merge_enabled=auto_ok,
    )
