from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Config:
    state_path: Path
    cache_path: Path
    archive_base_url: str
    mailing_list: str
    mail_lookback_minutes: int
    mail_max_messages: int
    commitfest_base_url: str
    commitfest_id: str
    postgres_git_url: str
    postgres_git_branch: str
    mail_poll_seconds: int
    commitfest_poll_seconds: int
    git_poll_seconds: int
    github_repository: str
    github_base_branch: str
    github_api_url: str
    github_token: str
    github_app_id: str
    github_app_installation_id: str
    github_app_private_key_path: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            state_path=Path(os.getenv("PG_HUB_STATE", ".pg_hub/state.db")),
            cache_path=Path(os.getenv("PG_HUB_CACHE", ".pg_hub/cache")),
            archive_base_url=os.getenv("PG_ARCHIVE_BASE_URL", "https://www.postgresql.org").rstrip("/"),
            mailing_list=os.getenv("PG_MAILING_LIST", "pgsql-hackers"),
            mail_lookback_minutes=_integer("PG_MAIL_LOOKBACK_MINUTES", 180),
            mail_max_messages=_integer("PG_MAIL_MAX_MESSAGES", 100),
            commitfest_base_url=os.getenv("COMMITFEST_BASE_URL", "https://commitfest.postgresql.org").rstrip("/"),
            commitfest_id=os.getenv("COMMITFEST_ID", "61"),
            postgres_git_url=os.getenv("POSTGRES_GIT_URL", "https://git.postgresql.org/git/postgresql.git"),
            postgres_git_branch=os.getenv("POSTGRES_GIT_BRANCH", "master"),
            mail_poll_seconds=_integer("MAIL_POLL_SECONDS", 600),
            commitfest_poll_seconds=_integer("COMMITFEST_POLL_SECONDS", 600),
            git_poll_seconds=_integer("GIT_POLL_SECONDS", 900),
            github_repository=os.getenv("GITHUB_TARGET_REPOSITORY", ""),
            github_base_branch=os.getenv("GITHUB_BASE_BRANCH", "master"),
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_app_id=os.getenv("GITHUB_APP_ID", ""),
            github_app_installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID", ""),
            github_app_private_key_path=os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", ""),
        )

    def validate_github_target(self) -> None:
        if not self.github_repository:
            raise ValueError("GITHUB_TARGET_REPOSITORY is required for the GitHub sink")
        normalized = self.github_repository.lower().strip("/")
        if normalized in {"postgres/postgres", "postgresql/postgresql"}:
            raise ValueError(
                "refusing to write to an upstream PostgreSQL repository; "
                "configure a user-controlled mirror"
            )
        if not (self.github_token or self.github_app_id):
            raise ValueError("set GITHUB_TOKEN or the GitHub App credentials")
        if not self.github_token:
            missing = [
                name
                for name, value in (
                    ("GITHUB_APP_INSTALLATION_ID", self.github_app_installation_id),
                    ("GITHUB_APP_PRIVATE_KEY_PATH", self.github_app_private_key_path),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"missing GitHub App settings: {', '.join(missing)}")
