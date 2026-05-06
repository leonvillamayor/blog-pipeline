"""Clasificación de commits/cambios en 'article' vs 'infra'.

Convención del modelo híbrido:
- Toca content/posts/<slug>/* → commit de artículo (promoción artículo-level)
- Toca layouts/, static/, themes/, hugo.toml, etc. → commit de infra
  (promoción branch-level via workflows existentes)
- Toca ambos → mixto (la GUI lo señala como tal y desaconseja cherry-pick)
"""

from __future__ import annotations

import re
from typing import Literal

CommitKind = Literal["article", "infra", "mixed", "other"]

_SLUG_RE = re.compile(r"^content/posts/(?P<slug>[^/]+)/")


def classify_paths(paths: list[str], article_prefix: str, infra_prefixes: tuple[str, ...]) -> tuple[CommitKind, set[str]]:
    """Devuelve (kind, set_de_slugs_tocados).

    kind:
      - "article": todos los paths tocan un artículo (puede ser uno o varios slugs).
      - "infra":   todos los paths son infra.
      - "mixed":   mezcla artículo + infra.
      - "other":   ninguno encaja (ficheros sueltos como .gitignore, README, etc.).
    """
    slugs: set[str] = set()
    n_article = n_infra = n_other = 0

    for path in paths:
        if path.startswith(article_prefix):
            m = _SLUG_RE.match(path)
            if m:
                slugs.add(m.group("slug"))
                n_article += 1
            else:
                n_other += 1
        elif any(path.startswith(p) for p in infra_prefixes):
            n_infra += 1
        else:
            n_other += 1

    if n_article > 0 and n_infra == 0:
        return "article", slugs
    if n_infra > 0 and n_article == 0:
        return "infra", slugs
    if n_article > 0 and n_infra > 0:
        return "mixed", slugs
    return "other", slugs
