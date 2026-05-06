"""Cliente HTTP para la GitHub REST API.

Operaciones: crear PRs, mergear (con auto-merge), borrar branches. El PAT
se inyecta en el header Authorization. Todas las llamadas son síncronas
y se ejecutan dentro de `asyncio.to_thread` desde los endpoints.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import json
from dataclasses import dataclass
from typing import Any


class GitHubError(RuntimeError):
    """Error de la API de GitHub."""

    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        super().__init__(f"GitHub {status}: {body}")


@dataclass
class GitHubClient:
    token: str
    repo: str  # "owner/name"

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        url = f"https://api.github.com{path}"
        r = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "blog-pipeline/0.1",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(r) as resp:
                txt = resp.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())
            except Exception:
                err = e.read().decode()
            raise GitHubError(e.code, err) from None

    # === Branches ===

    def get_branch_sha(self, branch: str) -> str:
        d = self._req("GET", f"/repos/{self.repo}/branches/{branch}")
        return d["commit"]["sha"]

    def delete_branch(self, branch: str) -> None:
        self._req("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")

    def branch_exists(self, branch: str) -> bool:
        try:
            self._req("GET", f"/repos/{self.repo}/branches/{branch}")
            return True
        except GitHubError as e:
            if e.status == 404:
                return False
            raise

    def create_branch(self, name: str, from_sha: str) -> dict:
        return self._req(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{name}", "sha": from_sha},
        )  # type: ignore[return-value]

    # === Tree / blobs / contents ===

    def get_tree(self, sha: str, recursive: bool = True) -> list[dict]:
        """Devuelve la lista de blobs del tree en una rama. recursive=True vital."""
        suffix = "?recursive=1" if recursive else ""
        d = self._req("GET", f"/repos/{self.repo}/git/trees/{sha}{suffix}")
        return d.get("tree", [])  # type: ignore[union-attr]

    def get_contents(self, path: str, ref: str) -> dict | None:
        """GET /contents/<path>?ref=<branch>. Devuelve dict con 'content' base64 y 'sha'.

        None si no existe.
        """
        try:
            return self._req("GET", f"/repos/{self.repo}/contents/{path}?ref={ref}")  # type: ignore[return-value]
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def put_contents(
        self,
        path: str,
        branch: str,
        content_b64: str,
        message: str,
        sha: str | None = None,
    ) -> dict:
        """PUT /contents/<path>. Crea o actualiza un fichero en `branch`.

        ⚠️ NO firma commits auto. Usar create_commit_on_branch() para
        commits firmados (requerido por branch protection).
        """
        body = {"message": message, "content": content_b64, "branch": branch}
        if sha is not None:
            body["sha"] = sha
        return self._req("PUT", f"/repos/{self.repo}/contents/{path}", body)  # type: ignore[return-value]

    def delete_contents(self, path: str, branch: str, sha: str, message: str) -> dict:
        """DELETE /contents/<path>. Borra un fichero en `branch`.

        ⚠️ NO firma commits auto. Usar create_commit_on_branch() para
        commits firmados.
        """
        return self._req(
            "DELETE",
            f"/repos/{self.repo}/contents/{path}",
            {"message": message, "branch": branch, "sha": sha},
        )  # type: ignore[return-value]

    def create_commit_on_branch(
        self,
        branch: str,
        message_headline: str,
        additions: list[dict] | None = None,
        deletions: list[dict] | None = None,
        message_body: str | None = None,
        expected_head_oid: str | None = None,
    ) -> dict:
        """GraphQL createCommitOnBranch — multi-file commit FIRMADO.

        A diferencia del REST Contents API, esta mutation hace que GitHub
        firme el commit con su web-flow GPG key (verified=true en el
        commit, satisface required_signatures).

        - additions: [{"path": "...", "contents": "<base64>"}]
        - deletions: [{"path": "..."}]
        - expected_head_oid: si se da, falla si HEAD ha cambiado (concurrencia)

        Devuelve {commit: {oid, url}}.
        """
        if not expected_head_oid:
            expected_head_oid = self.get_branch_sha(branch)

        owner, name = self.repo.split("/", 1)
        file_changes: dict = {}
        if additions:
            file_changes["additions"] = additions
        if deletions:
            file_changes["deletions"] = deletions

        msg_obj: dict = {"headline": message_headline}
        if message_body:
            msg_obj["body"] = message_body

        query = """
        mutation CreateCommit($input: CreateCommitOnBranchInput!) {
          createCommitOnBranch(input: $input) {
            commit { oid, url }
          }
        }
        """
        variables = {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": self.repo,
                    "branchName": branch,
                },
                "message": msg_obj,
                "fileChanges": file_changes,
                "expectedHeadOid": expected_head_oid,
            }
        }
        return self._gql(query, variables)["createCommitOnBranch"]["commit"]

    # === Pull Requests ===

    def create_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> dict:
        return self._req(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )  # type: ignore[return-value]

    def get_pr(self, number: int) -> dict:
        return self._req("GET", f"/repos/{self.repo}/pulls/{number}")  # type: ignore[return-value]

    def enable_auto_merge(self, pr_node_id: str, merge_method: str = "MERGE") -> dict:
        """Habilita auto-merge GraphQL. merge_method ∈ {MERGE, SQUASH, REBASE}.

        Por convención del proyecto, papers/articulos usan MERGE (no SQUASH)
        para mantener la genealogía vista en `feedback_promotion_merge`.
        """
        query = """
        mutation EnableAutoMerge($prId: ID!, $method: PullRequestMergeMethod!) {
          enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: $method}) {
            pullRequest { number, autoMergeRequest { enabledAt, mergeMethod } }
          }
        }
        """
        return self._gql(query, {"prId": pr_node_id, "method": merge_method})

    def merge_pr_now(self, number: int, merge_method: str = "merge") -> dict:
        """Merge inmediato (requiere checks ya verdes). merge_method ∈ {merge, squash, rebase}."""
        return self._req(
            "PUT",
            f"/repos/{self.repo}/pulls/{number}/merge",
            {"merge_method": merge_method},
        )  # type: ignore[return-value]

    # === GraphQL helper para auto-merge ===

    def _gql(self, query: str, variables: dict) -> dict:
        url = "https://api.github.com/graphql"
        body = json.dumps({"query": query, "variables": variables}).encode()
        r = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "blog-pipeline/0.1",
            },
        )
        try:
            with urllib.request.urlopen(r) as resp:
                d = json.loads(resp.read().decode())
                if "errors" in d:
                    raise GitHubError(200, d["errors"])
                return d["data"]
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode())
            except Exception:
                err = e.read().decode()
            raise GitHubError(e.code, err) from None
