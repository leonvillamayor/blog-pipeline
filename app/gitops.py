"""Wrapper sobre `git` para clonar, fetch y consultar estado del repo del blog.

Read-only en Fase 1: solo `git fetch` + lectura de árbol y commits. Las
operaciones de escritura (push, branch, merge) viven en `github_client.py`
y se ejecutan a través de la API de GitHub, no localmente, para que el
PAT controle scopes y deje audit log nativo.

Auth: usa git credential.helper=store con un fichero
~/.git-credentials (mode 600). Esto evita que el PAT aparezca en la
command-line de git (visible en `ps`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """Falla un comando git."""


def _run(args: list[str], cwd: Path, check: bool = True) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args)} → exit {res.returncode}: {res.stderr.strip()}")
    return res.stdout


def configure_credential_store(token: str, cred_file: Path) -> None:
    """Escribe credentials + gitconfig en `cred_file.parent`.

    Necesario antes del primer clone/fetch para que el PAT no aparezca en
    argv. Idempotente: sobreescribe los ficheros cada vez (rotaciones de PAT).

    Estrategia: usamos GIT_CONFIG_GLOBAL apuntando a un .gitconfig dedicado,
    en vez de tocar ~/.gitconfig (que en systemd con ProtectSystem=strict
    está read-only). Settea os.environ["GIT_CONFIG_GLOBAL"] para que todos
    los `git` siguientes hereden la config.
    """
    base = cred_file.parent
    base.mkdir(parents=True, exist_ok=True)

    # Credentials file (mode 600, contiene el PAT)
    cred_file.write_text(f"https://x-access-token:{token}@github.com\n")
    cred_file.chmod(0o600)

    # Gitconfig dedicado que apunta al credentials file
    gitconfig = base / ".gitconfig"
    gitconfig.write_text(
        "[credential]\n"
        f"\thelper = store --file={cred_file}\n"
    )
    gitconfig.chmod(0o600)

    # Apunta GIT_CONFIG_GLOBAL al fichero dedicado para que todos los
    # `git ...` siguientes lo lean. Evita tocar ~/.gitconfig.
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)


def ensure_clone(repo_path: Path, repo_url: str) -> None:
    """Clona el repo si no existe; si existe, no toca.

    repo_url debe ser la URL HTTPS limpia (sin token). El PAT se inyecta
    via credential.helper, configurado por configure_credential_store().
    """
    if (repo_path / ".git").exists():
        return
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--no-checkout", repo_url, str(repo_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def fetch_all(repo_path: Path) -> None:
    """git fetch --all --prune. Idempotente."""
    _run(["fetch", "--all", "--prune", "--quiet"], cwd=repo_path)


def list_branches(repo_path: Path, pattern: str | None = None) -> list[str]:
    """Lista refs remotos. Si pattern dado, filtra. Devuelve nombres sin 'origin/'."""
    out = _run(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"], cwd=repo_path)
    branches = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line == "origin/HEAD":
            continue
        name = line.removeprefix("origin/")
        if pattern and not name.startswith(pattern):
            continue
        branches.append(name)
    return sorted(branches)


def list_articles_in_branch(repo_path: Path, branch: str, article_prefix: str) -> list[str]:
    """Lista directorios de artículos presentes en una rama remota.

    Devuelve los nombres de carpeta dentro de article_prefix (ej. "content/posts/")
    que parecen ser bundles (contienen index.md o un .md hermano).
    """
    out = _run(
        ["ls-tree", "-r", "--name-only", f"origin/{branch}", "--", article_prefix],
        cwd=repo_path,
    )
    dirs: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line.endswith(".md"):
            continue
        rel = line.removeprefix(article_prefix).split("/", 1)[0]
        if not rel:
            continue
        # Saltar singletons que no son artículos (ej. _index.md, README.md)
        if rel.startswith("_") or rel.lower() in ("readme.md", "draft.md", "welcome.md"):
            continue
        dirs.add(rel)
    return sorted(dirs)


def read_file_at(repo_path: Path, branch: str, file_path: str) -> str | None:
    """Devuelve el contenido del fichero en `origin/<branch>:<file_path>`, o None."""
    try:
        return _run(["show", f"origin/{branch}:{file_path}"], cwd=repo_path, check=True)
    except GitError:
        return None


def commit_log(repo_path: Path, branch: str, max_count: int = 50) -> list[dict]:
    """Devuelve los últimos commits de origin/<branch> con sha, autor, fecha, asunto, paths tocados."""
    fmt = "%H%x09%an%x09%aI%x09%s"
    out = _run(
        [
            "log",
            f"origin/{branch}",
            f"--max-count={max_count}",
            f"--format={fmt}",
            "--name-only",
        ],
        cwd=repo_path,
    )
    commits = []
    cur: dict | None = None
    for line in out.splitlines():
        if "\t" in line and len(line.split("\t")) == 4:
            if cur is not None:
                commits.append(cur)
            sha, author, iso, subject = line.split("\t", 3)
            cur = {"sha": sha, "author": author, "date": iso, "subject": subject, "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur is not None:
        commits.append(cur)
    return commits
