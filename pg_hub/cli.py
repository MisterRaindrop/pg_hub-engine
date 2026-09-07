from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from .config import Config
from .github import GitHubSink
from .sink import ConsoleSink
from .sources import (
    CommitFestSource,
    FixtureSources,
    PostgresArchiveSource,
    PostgresGitSource,
)
from .state import StateStore
from .sync import SyncEngine, SyncReport


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = PROJECT_ROOT / "fixtures" / "demo.json"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="pg-hub", description="Read-only PostgreSQL → GitHub development mirror"
    )
    commands = result.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the zero-credential fixture demo")
    demo.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    demo.add_argument("--state", type=Path, default=Path(".pg_hub/demo.db"))
    demo.add_argument("--reset", action="store_true", help="remove only the selected demo state first")
    demo.add_argument("--twice", action="store_true", help="run twice to show the second pass is a no-op")

    sync = commands.add_parser("sync", help="run one incremental sync")
    sync.add_argument("--source", choices=("live", "fixture"), default="live")
    sync.add_argument("--sink", choices=("console", "github"), default="console")
    sync.add_argument("--only", choices=("all", "mail", "commitfest", "git"), default="all")
    sync.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)

    schedule = commands.add_parser("scheduler", help="run the 10/10/15 minute polling loop")
    schedule.add_argument("--sink", choices=("console", "github"), default="console")

    commands.add_parser("doctor", help="validate configuration without writing anywhere")
    return result


def _sources(config: Config, kind: str, fixture: Path = DEFAULT_FIXTURE):
    if kind == "fixture":
        return FixtureSources.load(fixture)
    return FixtureSources(
        mail=PostgresArchiveSource(config),
        commitfest=CommitFestSource(config),
        git=PostgresGitSource(config),
    )


def _sink(config: Config, kind: str):
    return GitHubSink(config) if kind == "github" else ConsoleSink()


def _print_report(report: SyncReport) -> None:
    print(
        "summary            "
        f"mail={report.mail_changed}/{report.mail_seen} "
        f"mail_skipped={report.mail_skipped} "
        f"commitfest={report.commitfest_changed}/{report.commitfest_seen} "
        f"git={report.git_changed}/{report.git_seen} changed={report.changed}"
    )


def _run_selected(engine: SyncEngine, sources, selected: str) -> SyncReport:
    if selected == "all":
        return engine.sync_all(sources.mail, sources.commitfest, sources.git)
    if selected == "mail":
        return engine.sync_mail(sources.mail)
    if selected == "commitfest":
        return engine.sync_commitfest(sources.commitfest)
    return engine.sync_git(sources.git)


def run_demo(args: argparse.Namespace) -> int:
    if args.reset and args.state.exists():
        args.state.unlink()
    config = Config.from_env()
    sources = _sources(config, "fixture", args.fixture)
    state = StateStore(args.state)
    try:
        print("pg_hub demo — local GitHub simulation")
        engine = SyncEngine(state, ConsoleSink())
        _print_report(engine.sync_all(sources.mail, sources.commitfest, sources.git))
        if args.twice:
            print("\nsecond pass — idempotency check")
            _print_report(engine.sync_all(sources.mail, sources.commitfest, sources.git))
    finally:
        state.close()
    return 0


def run_sync(args: argparse.Namespace) -> int:
    config = Config.from_env()
    sources = _sources(config, args.source, args.fixture)
    state = StateStore(config.state_path)
    try:
        report = _run_selected(SyncEngine(state, _sink(config, args.sink)), sources, args.only)
        _print_report(report)
    finally:
        state.close()
    return 0


def run_scheduler(args: argparse.Namespace) -> int:
    config = Config.from_env()
    sources = _sources(config, "live")
    state = StateStore(config.state_path)
    engine = SyncEngine(state, _sink(config, args.sink))
    intervals = {
        "mail": config.mail_poll_seconds,
        "commitfest": config.commitfest_poll_seconds,
        "git": config.git_poll_seconds,
    }
    next_run = {name: 0.0 for name in intervals}
    print(
        "scheduler started — "
        f"mail={intervals['mail']}s commitfest={intervals['commitfest']}s git={intervals['git']}s"
    )
    try:
        while True:
            now = time.monotonic()
            for name, interval in intervals.items():
                if now < next_run[name]:
                    continue
                try:
                    report = _run_selected(engine, sources, name)
                    _print_report(report)
                except Exception as error:  # keep the other read-only polls alive
                    print(f"{name} sync failed: {error}")
                next_run[name] = time.monotonic() + interval
            time.sleep(min(5, max(1, int(min(next_run.values()) - time.monotonic()))))
    except KeyboardInterrupt:
        print("scheduler stopped")
    finally:
        state.close()
    return 0


def run_doctor() -> int:
    config = Config.from_env()
    errors: list[str] = []
    if not (300 <= config.mail_poll_seconds <= 600):
        errors.append("MAIL_POLL_SECONDS should be between 300 and 600")
    if not (300 <= config.commitfest_poll_seconds <= 600):
        errors.append("COMMITFEST_POLL_SECONDS should be between 300 and 600")
    if not (600 <= config.git_poll_seconds <= 900):
        errors.append("GIT_POLL_SECONDS should be between 600 and 900")
    github_state = "not configured (console mode is available)"
    if config.github_repository or config.github_token or config.github_app_id:
        try:
            config.validate_github_target()
            github_state = f"configured for {config.github_repository}"
        except ValueError as error:
            errors.append(str(error))
    print(f"state               {config.state_path}")
    print(f"cache               {config.cache_path}")
    print(f"mail                {config.mailing_list} every {config.mail_poll_seconds}s")
    print(f"commitfest          {config.commitfest_id} every {config.commitfest_poll_seconds}s")
    print(f"postgres.git        {config.postgres_git_branch} every {config.git_poll_seconds}s")
    print(f"github              {github_state}")
    for error in errors:
        print(f"ERROR               {error}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    if args.command == "demo":
        return run_demo(args)
    if args.command == "sync":
        return run_sync(args)
    if args.command == "scheduler":
        return run_scheduler(args)
    return run_doctor()
