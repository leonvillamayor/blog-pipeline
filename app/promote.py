"""Operaciones de promoción y borrado artículo-level.

Usa GraphQL `createCommitOnBranch` para todas las escrituras: cada
mutation produce UN commit con MÚLTIPLES ficheros firmado por la
web-flow GPG key de GitHub, lo que satisface el requisito de branch
protection 'Commits must have verified signatures'.

Históricamente se intentó con REST Contents API (PUT /contents) pero
NO firma commits — los deja unsigned. Documentado en commit
'fix(security): refactorizar a GraphQL createCommitOnBranch'.

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
    """Promociona un artículo de `from_branch` a `to_branch` via PR.

    Pasos:
    1. Listar ficheros del slug en `from_branch`.
    2. Crear branch worker `auto/promote-<slug>-to-<to_branch>-<ts>` desde `to_branch`.
    3. Leer contenidos de los ficheros en `from_branch` (base64).
    4. UN solo commit firmado en el worker via createCommitOnBranch GraphQL,
       con todos los ficheros como additions.
    5. Crear PR worker → to_branch + auto-merge.
    """
    files = _list_article_files(gh, slug, from_branch)
    if not files:
        raise ValueError(f"No hay ficheros para slug='{slug}' en branch '{from_branch}'")

    ts = int(time.time())
    work = f"auto/promote-{slug}-to-{to_branch}-{ts}"
    base_sha = gh.get_branch_sha(to_branch)
    gh.create_branch(work, from_sha=base_sha)

    # Reunir contenidos de los ficheros desde from_branch
    additions = []
    for f in files:
        src = gh.get_contents(f["path"], ref=from_branch)
        if src is None or "content" not in src:
            continue
        # GitHub devuelve el content en base64 con saltos de línea.
        # createCommitOnBranch acepta el base64 como string sin filtrar.
        content_clean = src["content"].replace("\n", "").replace("\r", "")
        additions.append({"path": f["path"], "contents": content_clean})

    if not additions:
        gh.delete_branch(work)
        raise RuntimeError(f"No se pudo leer contenido de ningún fichero para slug='{slug}'")

    # UN commit firmado con todos los ficheros
    headline = f"promote({slug}): {from_branch} → {to_branch}"
    msg_body = (
        f"Promoción artículo-level de `{slug}`.\n\n"
        f"- Origen: `{from_branch}`\n"
        f"- Destino: `{to_branch}`\n"
        f"- Ficheros: {len(additions)}\n\n"
        "Generado por blog-pipeline GUI."
    )
    gh.create_commit_on_branch(
        branch=work,
        message_headline=headline,
        message_body=msg_body,
        additions=additions,
    )

    # Crear PR
    title = f"🚀 {headline}"
    body = (
        f"Promoción artículo-level del slug `{slug}`.\n\n"
        f"- Origen: `{from_branch}`\n"
        f"- Destino: `{to_branch}`\n"
        f"- Ficheros: {len(additions)}\n"
        f"- Worker branch: `{work}`\n\n"
        "Generado por blog-pipeline GUI. Commit firmado vía GraphQL "
        "`createCommitOnBranch` (web-flow GPG key). Merge=MERGE per "
        "memory `feedback_promotion_merge`."
    )
    pr = gh.create_pr(head=work, base=to_branch, title=title, body=body)

    auto_ok = True
    try:
        gh.enable_auto_merge(pr["node_id"], merge_method="MERGE")
    except GitHubError:
        auto_ok = False

    return PromoteResult(
        pr_number=pr["number"],
        pr_url=pr["html_url"],
        work_branch=work,
        files_committed=len(additions),
        auto_merge_enabled=auto_ok,
    )


def delete_article(
    gh: GitHubClient,
    slug: str,
    from_branch: str,
) -> PromoteResult:
    """Borra el artículo de `from_branch` via PR (UN commit firmado)."""
    files = _list_article_files(gh, slug, from_branch)
    if not files:
        raise ValueError(f"No hay ficheros para slug='{slug}' en branch '{from_branch}'")

    ts = int(time.time())
    work = f"auto/delete-{slug}-from-{from_branch}-{ts}"
    base_sha = gh.get_branch_sha(from_branch)
    gh.create_branch(work, from_sha=base_sha)

    deletions = [{"path": f["path"]} for f in files]

    headline = f"delete({slug}) from {from_branch}"
    msg_body = (
        f"Borrado artículo-level de `{slug}` de `{from_branch}`.\n\n"
        f"- Ficheros borrados: {len(deletions)}\n\n"
        "Generado por blog-pipeline GUI."
    )
    gh.create_commit_on_branch(
        branch=work,
        message_headline=headline,
        message_body=msg_body,
        deletions=deletions,
    )

    title = f"🗑️ {headline}"
    body = (
        f"Borrado artículo-level del slug `{slug}`.\n\n"
        f"- Branch afectada: `{from_branch}`\n"
        f"- Ficheros borrados: {len(deletions)}\n"
        f"- Worker branch: `{work}`\n\n"
        "Generado por blog-pipeline GUI. Commit firmado vía GraphQL."
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
        files_committed=len(deletions),
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
