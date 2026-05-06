"""Modelo del estado del pipeline: qué artículo está en qué columna,
qué builds están en curso, qué cambios infra-level están pendientes.

Lee el repo clonado (gestionado por gitops.fetch_all) y construye:

   pending     : feature branches drafts/<slug>
   dev         : artículos en origin/dev (con su flag draft)
   preprod     : artículos en origin/preprod
   prod        : artículos en origin/main
   builds      : estado del último deploy de cada proyecto CF Pages
   infra_diff  : commits no-artículo entre branches (branch-level)

La GUI consume esta estructura.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import classifier, gitops
from .cloudflare_client import CloudflareClient, DeploymentStatus, CloudflareError
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
class GuiPR:
    """PR abierto creado por la GUI (head matchea auto/promote-* o auto/delete-*)."""
    number: int
    title: str
    head: str
    base: str
    url: str
    slug: str | None  # extraído del nombre de la branch
    check_state: str  # success | failure | pending | error
    check_summary: str  # "8/8 ✓" o "3/8 (1 fail)" etc.
    auto_merge: bool


@dataclass
class InfraDiff:
    """Commits no-artículo (infra) entre dos branches: tipo + slugs/paths involucrados."""
    from_branch: str
    to_branch: str
    commits: list[dict] = field(default_factory=list)  # cada uno con kind, sha, msg, files


@dataclass
class PipelineState:
    pending: list[Article] = field(default_factory=list)
    in_dev: list[Article] = field(default_factory=list)
    in_preprod: list[Article] = field(default_factory=list)
    in_prod: list[Article] = field(default_factory=list)
    builds: dict[str, DeploymentStatus | None] = field(default_factory=dict)
    infra_diff_dev_preprod: InfraDiff | None = None
    infra_diff_preprod_main: InfraDiff | None = None
    gui_prs: list[GuiPR] = field(default_factory=list)
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

    # Builds CF Pages — read-only, opcional
    builds: dict[str, DeploymentStatus | None] = {}
    if settings.cloudflare_token and settings.cloudflare_account_id:
        cf = CloudflareClient(
            token=settings.cloudflare_token,
            account_id=settings.cloudflare_account_id,
        )
        for proj in ("blog-dev", "blog-preprod", "blog-prod"):
            try:
                builds[proj] = cf.latest_deployment(proj)
            except CloudflareError:
                builds[proj] = None

    # Infra diff entre branches: commits no-artículo pendientes de promoción
    def _infra_commits(from_b: str, to_b: str) -> list[dict]:
        """Commits en from_b sin estar en to_b, clasificando por path."""
        try:
            out = gitops._run(
                ["log", f"origin/{to_b}..origin/{from_b}",
                 "--format=%H%x09%an%x09%aI%x09%s", "--name-only", "--max-count=30"],
                cwd=repo,
                check=False,
            )
        except gitops.GitError:
            return []
        commits: list[dict] = []
        cur: dict | None = None
        for line in out.splitlines():
            if "\t" in line and len(line.split("\t")) == 4:
                if cur is not None:
                    commits.append(cur)
                sha, author, iso, subject = line.split("\t", 3)
                cur = {"sha": sha[:8], "author": author, "iso": iso, "subject": subject, "files": [], "kind": "other"}
            elif line.strip() and cur is not None:
                cur["files"].append(line.strip())
        if cur is not None:
            commits.append(cur)
        # Clasificar
        for c in commits:
            kind, _ = classifier.classify_paths(
                c["files"],
                article_prefix=settings.article_path_prefix,
                infra_prefixes=settings.infra_paths,
            )
            c["kind"] = kind
        return commits

    infra_dev_preprod = InfraDiff(
        from_branch="dev",
        to_branch="preprod",
        commits=_infra_commits("dev", "preprod"),
    )
    infra_preprod_main = InfraDiff(
        from_branch="preprod",
        to_branch="main",
        commits=_infra_commits("preprod", "main"),
    )

    # PRs abiertos creados por la GUI (head=auto/*)
    gui_prs: list[GuiPR] = []
    if settings.github_token:
        from .github_client import GitHubClient as GHC, GitHubError as GHE
        gh = GHC(token=settings.github_token, repo=settings.blog_repo)
        try:
            raw_prs = gh.list_open_prs_by_head_pattern("auto/")
            import re as _re
            slug_re = _re.compile(r"^auto/(promote|delete)-(?P<slug>.+?)-(?:to|from)-")
            for pr in raw_prs:
                head = pr["head"]["ref"]
                m = slug_re.match(head)
                slug = m.group("slug") if m else None
                try:
                    chk = gh.pr_check_summary(pr["number"])
                    state_str = chk["state"].lower()
                    if chk["fail"] > 0:
                        summary = f"{chk['ok']}/{chk['total']} ({chk['fail']} fail)"
                    elif chk["pending"] > 0:
                        summary = f"{chk['ok']}/{chk['total']} ({chk['pending']} pending)"
                    elif chk["total"] == 0:
                        summary = "no checks"
                    else:
                        summary = f"{chk['ok']}/{chk['total']} ✓"
                    auto_m = chk["auto_merge"]
                except GHE:
                    state_str = "pending"
                    summary = "?"
                    auto_m = False
                gui_prs.append(GuiPR(
                    number=pr["number"],
                    title=pr["title"],
                    head=head,
                    base=pr["base"]["ref"],
                    url=pr["html_url"],
                    slug=slug,
                    check_state=state_str,
                    check_summary=summary,
                    auto_merge=auto_m,
                ))
        except Exception:
            gui_prs = []

    from datetime import datetime, timezone
    return PipelineState(
        pending=pending,
        in_dev=in_dev_only,
        in_preprod=in_preprod_only,
        in_prod=in_prod,
        builds=builds,
        infra_diff_dev_preprod=infra_dev_preprod,
        infra_diff_preprod_main=infra_preprod_main,
        gui_prs=gui_prs,
        last_refreshed_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
