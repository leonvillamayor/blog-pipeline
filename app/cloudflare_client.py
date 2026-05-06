"""Cliente para Cloudflare Pages API — solo consultas read-only.

Read-only: la GUI necesita el estado de los builds de los 3 proyectos
(blog-dev / blog-preprod / blog-prod) para mostrarlos por columna.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class CloudflareError(RuntimeError):
    pass


@dataclass
class DeploymentStatus:
    project: str
    commit_sha: str
    commit_message: str
    branch: str
    stage: str          # queued | initialize | clone_repo | build | deploy
    status: str         # idle | active | success | failure | canceled | skipped
    created_on: str
    url: str | None     # preview URL


@dataclass
class CloudflareClient:
    token: str
    account_id: str

    def _req(self, path: str) -> dict:
        url = f"https://api.cloudflare.com/client/v4{path}"
        r = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())
            except Exception:
                err = {"errors": [{"message": str(e)}]}
            raise CloudflareError(f"CF {e.code}: {err}") from None

    def latest_deployment(self, project: str) -> DeploymentStatus | None:
        d = self._req(
            f"/accounts/{self.account_id}/pages/projects/{project}/deployments?per_page=1",
        )
        results = d.get("result") or []
        if not results:
            return None
        dep = results[0]
        meta = (dep.get("deployment_trigger") or {}).get("metadata") or {}
        latest = dep.get("latest_stage") or {}
        return DeploymentStatus(
            project=project,
            commit_sha=(meta.get("commit_hash") or "?")[:8],
            commit_message=(meta.get("commit_message") or "?").split("\n")[0][:80],
            branch=meta.get("branch") or "?",
            stage=latest.get("name") or "?",
            status=latest.get("status") or "?",
            created_on=(dep.get("created_on") or "")[:19],
            url=dep.get("url"),
        )
