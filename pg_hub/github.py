from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import Config
from .errors import PatchSetUnavailableError
from .labels import definition
from .models import Attachment


_PATCH_SEQUENCE = re.compile(r"(?:^|[-_])(\d{4})(?=[-_])")
_POSTGRES_TARGET = re.compile(r"(?:^|[-_])PG(\d+)(?=[-_])", re.I)


def order_patch_attachments(
    attachments: tuple[Attachment, ...], base_branch: str
) -> tuple[Attachment, ...]:
    """Choose the target-specific series and order it by patch number."""
    branch_major = re.search(r"(?:REL_|PG)?(\d+)", base_branch, re.I)
    target_major = branch_major.group(1) if branch_major else None

    tagged: list[tuple[Attachment, str | None]] = []
    for attachment in attachments:
        match = _POSTGRES_TARGET.search(attachment.name)
        tagged.append((attachment, match.group(1) if match else None))

    if target_major:
        selected = [item for item, major in tagged if major == target_major]
    else:
        # PostgreSQL's master branch normally ships beside PG17/PG18 backpatch
        # variants without an explicit PG target in its filename.
        selected = [item for item, major in tagged if major is None]
    if not selected:
        selected = [item for item, _ in tagged]

    def key(item: Attachment) -> tuple[int, str]:
        match = _PATCH_SEQUENCE.search(item.name)
        return (int(match.group(1)) if match else 10_000, item.name.lower())

    return tuple(sorted(selected, key=key))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class GitHubAuth:
    def __init__(self, config: Config):
        self.config = config
        self._token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def token(self) -> str:
        if self.config.github_token:
            return self.config.github_token
        if self._token and datetime.now(timezone.utc) < self._expires_at - timedelta(minutes=2):
            return self._token
        self._token, self._expires_at = self._exchange_app_token()
        return self._token

    def _exchange_app_token(self) -> tuple[str, datetime]:
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_ID", self.config.github_app_id),
                ("GITHUB_APP_INSTALLATION_ID", self.config.github_app_installation_id),
                ("GITHUB_APP_PRIVATE_KEY_PATH", self.config.github_app_private_key_path),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing GitHub App settings: {', '.join(missing)}")
        now = datetime.now(timezone.utc)
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        payload = _b64url(
            json.dumps(
                {
                    "iat": int((now - timedelta(seconds=30)).timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": self.config.github_app_id,
                }
            ).encode()
        )
        unsigned = f"{header}.{payload}".encode("ascii")
        result = subprocess.run(
            [
                "openssl", "dgst", "-sha256", "-sign",
                self.config.github_app_private_key_path,
            ],
            input=unsigned,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"could not sign GitHub App JWT: {result.stderr.decode().strip()}")
        jwt = f"{header}.{payload}.{_b64url(result.stdout)}"
        url = (
            f"{self.config.github_api_url}/app/installations/"
            f"{quote(self.config.github_app_installation_id)}/access_tokens"
        )
        request = Request(
            url,
            data=b"{}",
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pg_hub/0.1",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=30) as response:
            document = json.loads(response.read())
        expires_at = datetime.fromisoformat(document["expires_at"].replace("Z", "+00:00"))
        return str(document["token"]), expires_at


class GitHubClient:
    def __init__(self, config: Config, auth: GitHubAuth):
        self.config = config
        self.auth = auth
        self.repo_path = f"repos/{config.github_repository}"

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        url = f"{self.config.github_api_url}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.auth.token()}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pg_hub/0.1",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed ({error.code}): {detail}") from error
        return json.loads(raw) if raw else None

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        document = self.request(
            "POST", "graphql", {"query": query, "variables": variables}
        )
        if document.get("errors"):
            raise RuntimeError(f"GitHub GraphQL failed: {document['errors']}")
        return dict(document["data"])

    def ensure_label(self, name: str) -> None:
        color, description = definition(name)
        encoded = quote(name, safe="")
        try:
            self.request("GET", f"{self.repo_path}/labels/{encoded}")
        except RuntimeError as error:
            if "(404)" not in str(error):
                raise
            self.request(
                "POST",
                f"{self.repo_path}/labels",
                {"name": name, "color": color, "description": description},
            )

    def ensure_pull_request(self, title: str, body: str, branch: str) -> int:
        owner = self.config.github_repository.split("/", 1)[0]
        pulls = self.request(
            "GET",
            f"{self.repo_path}/pulls?state=open&head={quote(owner + ':' + branch)}&per_page=10",
        )
        if pulls:
            number = int(pulls[0]["number"])
            self.request(
                "PATCH", f"{self.repo_path}/pulls/{number}",
                {"title": title, "body": body},
            )
            return number
        created = self.request(
            "POST",
            f"{self.repo_path}/pulls",
            {
                "title": title,
                "body": body,
                "head": branch,
                "base": self.config.github_base_branch,
                "maintainer_can_modify": False,
            },
        )
        return int(created["number"])

    def ensure_issue(self, title: str, body: str, marker: str) -> int:
        issues = self.request("GET", f"{self.repo_path}/issues?state=all&per_page=100")
        for issue in issues:
            if "pull_request" in issue or marker not in str(issue.get("body", "")):
                continue
            number = int(issue["number"])
            self.request(
                "PATCH", f"{self.repo_path}/issues/{number}",
                {"title": title, "body": body, "state": "open"},
            )
            return number
        created = self.request(
            "POST", f"{self.repo_path}/issues", {"title": title, "body": body}
        )
        return int(created["number"])

    def _discussion_repository(self) -> dict[str, Any]:
        owner, name = self.config.github_repository.split("/", 1)
        data = self.graphql(
            """
            query($owner: String!, $name: String!) {
              repository(owner: $owner, name: $name) {
                id
                discussionCategories(first: 25) { nodes { id name slug } }
                discussions(first: 100) { nodes { id number title body } }
              }
            }
            """,
            {"owner": owner, "name": name},
        )
        return dict(data["repository"])

    def ensure_discussion(self, title: str, body: str, marker: str) -> int:
        repository = self._discussion_repository()
        for discussion in repository["discussions"]["nodes"]:
            if marker not in str(discussion.get("body", "")):
                continue
            self.graphql(
                """
                mutation($id: ID!, $title: String!, $body: String!) {
                  updateDiscussion(input: {discussionId: $id, title: $title, body: $body}) {
                    discussion { number }
                  }
                }
                """,
                {"id": discussion["id"], "title": title, "body": body},
            )
            return int(discussion["number"])
        categories = repository["discussionCategories"]["nodes"]
        category = next(
            (item for item in categories if item["slug"] == "announcements"),
            categories[0] if categories else None,
        )
        if category is None:
            raise RuntimeError("GitHub Discussions has no available category")
        data = self.graphql(
            """
            mutation($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
              createDiscussion(input: {
                repositoryId: $repositoryId,
                categoryId: $categoryId,
                title: $title,
                body: $body
              }) { discussion { number } }
            }
            """,
            {
                "repositoryId": repository["id"],
                "categoryId": category["id"],
                "title": title,
                "body": body,
            },
        )
        return int(data["createDiscussion"]["discussion"]["number"])

    def add_comment(self, pr_number: int, body: str, marker: str) -> None:
        comments = self.request(
            "GET", f"{self.repo_path}/issues/{pr_number}/comments?per_page=100"
        )
        mutation: tuple[str, str, dict[str, str]] | None = None
        for comment in comments:
            existing_body = str(comment.get("body", ""))
            if marker not in existing_body:
                continue
            if existing_body != body:
                mutation = (
                    "PATCH",
                    f"{self.repo_path}/issues/comments/{comment['id']}",
                    {"body": body},
                )
            else:
                return
            break
        if mutation is None:
            mutation = (
                "POST",
                f"{self.repo_path}/issues/{pr_number}/comments",
                {"body": body},
            )

        issue = self.request("GET", f"{self.repo_path}/issues/{pr_number}")
        was_locked = bool(issue.get("locked"))
        if was_locked:
            self.request("DELETE", f"{self.repo_path}/issues/{pr_number}/lock")
        try:
            self.request(*mutation)
        finally:
            if was_locked:
                self.request("PUT", f"{self.repo_path}/issues/{pr_number}/lock", {})

    def _discussion(self, number: int, include_comments: bool = False) -> dict[str, Any]:
        owner, name = self.config.github_repository.split("/", 1)
        comments = "comments(first: 100) { nodes { id body } }" if include_comments else ""
        data = self.graphql(
            f"""
            query($owner: String!, $name: String!, $number: Int!) {{
              repository(owner: $owner, name: $name) {{
                discussion(number: $number) {{ id number locked {comments} }}
              }}
            }}
            """,
            {"owner": owner, "name": name, "number": number},
        )
        discussion = data["repository"]["discussion"]
        if discussion is None:
            raise RuntimeError(f"GitHub discussion #{number} was not found")
        return dict(discussion)

    def add_discussion_comment(self, number: int, body: str, marker: str) -> None:
        discussion = self._discussion(number, include_comments=True)
        mutation: tuple[str, dict[str, str]] | None = None
        for comment in discussion["comments"]["nodes"]:
            existing_body = str(comment.get("body", ""))
            if marker not in existing_body:
                continue
            if existing_body != body:
                mutation = (
                    """
                    mutation($id: ID!, $body: String!) {
                      updateDiscussionComment(input: {commentId: $id, body: $body}) {
                        comment { id }
                      }
                    }
                    """,
                    {"id": comment["id"], "body": body},
                )
            else:
                return
            break
        if mutation is None:
            mutation = (
                """
                mutation($discussionId: ID!, $body: String!) {
                  addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
                    comment { id }
                  }
                }
                """,
                {"discussionId": discussion["id"], "body": body},
            )

        was_locked = bool(discussion.get("locked"))
        if was_locked:
            self._set_discussion_lock(discussion["id"], locked=False)
        try:
            self.graphql(*mutation)
        finally:
            if was_locked:
                self._set_discussion_lock(discussion["id"], locked=True)

    def _set_discussion_lock(self, discussion_id: str, locked: bool) -> None:
        operation = "lockLockable" if locked else "unlockLockable"
        self.graphql(
            f"""
            mutation($id: ID!) {{
              {operation}(input: {{lockableId: $id}}) {{
                clientMutationId
              }}
            }}
            """,
            {"id": discussion_id},
        )

    def lock(self, kind: str, number: int) -> None:
        if kind in {"pr", "issue"}:
            self.request("PUT", f"{self.repo_path}/issues/{number}/lock", {})
            return
        discussion = self._discussion(number)
        if not discussion.get("locked"):
            self._set_discussion_lock(discussion["id"], locked=True)

    def link_discussion_pr(
        self, discussion_number: int, pr_number: int, marker: str
    ) -> None:
        repository_url = f"https://github.com/{self.config.github_repository}"
        self.add_comment(
            pr_number,
            (
                f"Earlier design discussion: [Discussion #{discussion_number}]"
                f"({repository_url}/discussions/{discussion_number})\n\n{marker}"
            ),
            marker,
        )
        discussion_marker = marker.replace("=", "-discussion=", 1)
        self.add_discussion_comment(
            discussion_number,
            (
                f"A patch is now available as [PR #{pr_number}]"
                f"({repository_url}/pull/{pr_number}).\n\n{discussion_marker}"
            ),
            discussion_marker,
        )

    def sync_labels(self, pr_number: int, labels: Iterable[str]) -> None:
        wanted = sorted(set(labels))
        for label in wanted:
            self.ensure_label(label)
        current = self.request("GET", f"{self.repo_path}/issues/{pr_number}/labels")
        managed_prefixes = ("source:", "type:", "area:", "status:", "cf:", "upstream:")
        preserved = [
            item["name"]
            for item in current
            if not str(item["name"]).startswith(managed_prefixes)
        ]
        self.request(
            "PUT",
            f"{self.repo_path}/issues/{pr_number}/labels",
            {"labels": sorted(set(preserved) | set(wanted))},
        )

    def ensure_milestone(self, title: str) -> int:
        milestones = self.request(
            "GET", f"{self.repo_path}/milestones?state=all&per_page=100"
        )
        for milestone in milestones:
            if milestone["title"] == title:
                return int(milestone["number"])
        created = self.request(
            "POST",
            f"{self.repo_path}/milestones",
            {"title": title, "description": "Mirrored PostgreSQL CommitFest milestone"},
        )
        return int(created["number"])

    def set_milestone(self, pr_number: int, title: str) -> None:
        number = self.ensure_milestone(title)
        self.request(
            "PATCH", f"{self.repo_path}/issues/{pr_number}", {"milestone": number}
        )


class PatchPublisher:
    def __init__(self, config: Config, auth: GitHubAuth):
        self.config = config
        self.auth = auth
        self.checkout = config.cache_path / "postgres-work"

    def branch_for(self, thread_id: str) -> str:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
        return f"pg-hub/mirror-patch-{digest}"

    def publish(self, thread_id: str, attachments: tuple[Attachment, ...]) -> str:
        if not attachments:
            raise ValueError("cannot publish a patch branch without patch attachments")
        self._prepare_checkout()
        branch = self.branch_for(thread_id)
        with tempfile.TemporaryDirectory(prefix="pg-hub-patch-") as temp:
            temp_path = Path(temp)
            worktree = temp_path / "worktree"
            self._run(
                "git", "-C", str(self.checkout), "worktree", "add", "--detach",
                str(worktree), f"origin/{self.config.github_base_branch}",
            )
            try:
                self._run("git", "-C", str(worktree), "switch", "-C", branch)
                patches = self._download_patches(
                    order_patch_attachments(attachments, self.config.github_base_branch),
                    temp_path,
                )
                self._apply_patchset(worktree, patches)
                self._reject_workflow_changes(worktree)
                self._push(worktree, branch)
            finally:
                subprocess.run(
                    ["git", "-C", str(self.checkout), "worktree", "remove", "--force", str(worktree)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        return branch

    def _prepare_checkout(self) -> None:
        self.checkout.parent.mkdir(parents=True, exist_ok=True)
        mirror_url = f"https://github.com/{self.config.github_repository}.git"
        if not self.checkout.exists():
            self._run(
                "git", "clone", "--filter=blob:none", "--depth=1", "--no-checkout",
                "--branch", self.config.github_base_branch,
                mirror_url, str(self.checkout),
            )
        self._run(
            "git", "-C", str(self.checkout), "fetch", "--quiet", "origin",
            self.config.github_base_branch,
        )

    @staticmethod
    def _download_patches(
        attachments: tuple[Attachment, ...], destination: Path
    ) -> list[Path]:
        paths: list[Path] = []
        for index, attachment in enumerate(attachments):
            safe_name = Path(attachment.name).name
            path = destination / f"{index:04d}-{safe_name}"
            request = Request(
                attachment.effective_download_url,
                method="GET",
                headers={"User-Agent": "pg_hub/0.1"},
            )
            with urlopen(request, timeout=60) as response:
                content = response.read()
            if not content:
                raise PatchSetUnavailableError(
                    f"PostgreSQL archive returned an empty attachment: {attachment.name}"
                )
            path.write_bytes(content)
            paths.append(path)
        return paths

    def _apply_patchset(self, worktree: Path, patches: list[Path]) -> None:
        am = subprocess.run(
            [
                "git", "-C", str(worktree),
                "-c", "user.name=pg_hub",
                "-c", "user.email=pg-hub@invalid.example",
                "am", "--3way", *map(str, patches),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if am.returncode == 0:
            return
        subprocess.run(
            ["git", "-C", str(worktree), "am", "--abort"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        for patch in patches:
            try:
                self._run("git", "-C", str(worktree), "apply", "--index", str(patch))
            except RuntimeError as error:
                raise PatchSetUnavailableError(
                    f"could not apply patch attachment {patch.name}: {error}"
                ) from error
        self._run(
            "git", "-C", str(worktree),
            "-c", "user.name=pg_hub",
            "-c", "user.email=pg-hub@invalid.example",
            "commit", "-m", "Apply mirrored PostgreSQL patch set",
        )

    def _reject_workflow_changes(self, worktree: Path) -> None:
        base = f"origin/{self.config.github_base_branch}"
        changed = self._run(
            "git", "-C", str(worktree), "diff", "--name-only",
            f"{base}...HEAD", "--", ".github/workflows",
        ).splitlines()
        if changed:
            paths = ", ".join(changed)
            raise PatchSetUnavailableError(
                "patch modifies protected GitHub Actions workflow files; "
                f"the read-only mirror will not publish them: {paths}"
            )

    def _push(self, worktree: Path, branch: str) -> None:
        remote = f"https://github.com/{self.config.github_repository}.git"
        token = self.auth.token()
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
        result = subprocess.run(
            [
                "git", "-C", str(worktree), "push", "--force",
                remote, f"HEAD:refs/heads/{branch}",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"could not publish mirror branch: {result.stderr.strip()}")

    @staticmethod
    def _run(*args: str) -> str:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"git command failed: {detail}")
        return result.stdout


@dataclass
class GitHubSink:
    config: Config

    def __post_init__(self) -> None:
        self.config.validate_github_target()
        self.auth = GitHubAuth(self.config)
        self.client = GitHubClient(self.config, self.auth)
        self.publisher = PatchPublisher(self.config, self.auth)

    def branch_for(self, thread_id: str) -> str:
        return self.publisher.branch_for(thread_id)

    def publish_patchset(self, thread_id: str, attachments: tuple[Attachment, ...]) -> str:
        return self.publisher.publish(thread_id, attachments)

    def ensure_pr(self, title: str, body: str, branch: str) -> int:
        return self.client.ensure_pull_request(title, body, branch)

    def ensure_issue(self, title: str, body: str, marker: str) -> int:
        return self.client.ensure_issue(title, body, marker)

    def ensure_discussion(self, title: str, body: str, marker: str) -> int:
        return self.client.ensure_discussion(title, body, marker)

    def comment(self, pr_number: int, body: str, marker: str) -> None:
        self.client.add_comment(pr_number, body, marker)

    def discussion_comment(self, number: int, body: str, marker: str) -> None:
        self.client.add_discussion_comment(number, body, marker)

    def labels(self, pr_number: int, labels: Iterable[str]) -> None:
        self.client.sync_labels(pr_number, labels)

    def milestone(self, pr_number: int, title: str) -> None:
        self.client.set_milestone(pr_number, title)

    def lock(self, kind: str, number: int) -> None:
        self.client.lock(kind, number)

    def link_discussion_pr(self, discussion_number: int, pr_number: int, marker: str) -> None:
        self.client.link_discussion_pr(discussion_number, pr_number, marker)
