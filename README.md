# pg_hub

**PG Hub — PostgreSQL Development Mirror** turns PostgreSQL's mailing-list
workflow into a GitHub-native, read-only view.

It reads development activity from
[pgsql-hackers](https://www.postgresql.org/list/pgsql-hackers/),
[PostgreSQL CommitFest](https://commitfest.postgresql.org/), and
[postgres.git](https://git.postgresql.org/gitweb/?p=postgresql.git;a=summary).
It writes only to a GitHub repository you control. There is no code path that
posts mail, edits CommitFest, or pushes to PostgreSQL upstream.

> **Demo status:** useful end-to-end scaffold, not a production mirror. Start
> with the local fixture, then point it at a disposable PostgreSQL fork/mirror.

## What the demo does

- A `[PATCH]` email thread with a `.patch`/`.diff` attachment becomes one PR.
- Later messages in that thread become attributed PR comments.
- A newer patch attachment rebuilds the same `pg-hub/patch-*` branch, so GitHub
  exposes the patch under **Files changed**.
- CommitFest status becomes a `status:*` label and the active CommitFest becomes
  a milestone.
- Simple keyword rules add subsystem labels such as `area:storage`, `area:wal`,
  `area:replication`, `area:vacuum`, `area:planner`, and `area:executor`.
- Every PR and comment links back to the original PostgreSQL email.
- A matching postgres.git commit adds `upstream:committed` and an informational
  comment. The mirror deliberately does not merge or close the PR.

The first version does **not** support GitHub comment → email, GitHub reviews →
pgsql-hackers, creating upstream bugs, merging/closing upstream work, changing
CommitFest state, contributor login, identity federation, or an AI agent.

## Run the zero-credential demo

Python 3.11+ and Git are the only requirements; there are no Python package
dependencies.

```bash
make demo
```

Or directly:

```bash
python3 -m pg_hub demo --reset --twice
```

The first pass simulates publishing one PR, two versions of its patch branch,
mail comments, CommitFest labels/milestone, and a matching upstream commit. The
second pass reports `changed=0`, demonstrating durable idempotency.

Run the tests and configuration check with:

```bash
make test
make doctor
```

## Data flow

```text
pgsql-hackers incremental archive
  Message-ID + thread root + attachment URL
                  │
CommitFest snapshot ── patch id + status + tags
                  │
postgres.git ───────── commit hash
                  ▼
            SQLite state store
       cursors + fingerprints + mapping
                  │
                  ▼
     user-controlled PostgreSQL mirror
       PR + comments + branch + labels
```

The SQLite state file is not merely a cache. It stores:

- a high-water cursor for each source;
- one fingerprint per `Message-ID`;
- one fingerprint per CommitFest patch id;
- one record per postgres.git commit hash;
- the thread → PR/branch and CommitFest patch → thread mappings.

Source cursors overlap slightly on every mail poll; stable IDs make that safe.
GitHub PR branches and hidden comment markers provide a second idempotency layer
if a job succeeds remotely but loses its local state before checkpointing.

## Live read-only polling

Copy the example configuration, adjust the active CommitFest id, and first use
the console sink:

```bash
cp .env.example .env
python3 -m pg_hub sync --source live --sink console
```

The long-running scheduler follows the requested cadence by default:

```bash
python3 -m pg_hub scheduler --sink console
```

| Source | Default interval | Incremental key |
| --- | ---: | --- |
| pgsql-hackers | 10 minutes | Message-ID + time cursor |
| CommitFest | 10 minutes | patch id + content fingerprint |
| postgres.git | 15 minutes | commit hash |

This is high-frequency incremental polling, not a daily batch. The included
GitHub Actions workflow runs all three every 10 minutes, which is inside the
requested 5–10 / 10–15 minute windows. Scheduled Actions can occasionally be
delayed by GitHub, so a small always-on service is preferable for tighter
latency.

Useful one-source checks:

```bash
python3 -m pg_hub sync --source live --sink console --only mail
python3 -m pg_hub sync --source live --sink console --only commitfest
python3 -m pg_hub sync --source live --sink console --only git
```

## Connect a GitHub mirror

The target must be a PostgreSQL fork/mirror with a base branch compatible with
the configured postgres.git branch. `pg_hub` refuses the known upstream GitHub
repository names as write targets.

Set these values in `.env`:

```dotenv
GITHUB_TARGET_REPOSITORY=your-org/postgresql-mirror
GITHUB_BASE_BRANCH=master
GITHUB_TOKEN=github_pat_or_installation_token
```

Then validate before allowing writes:

```bash
python3 -m pg_hub doctor
python3 -m pg_hub sync --source live --sink github
```

For a classic/fine-grained PAT, grant only the target repository permissions
needed for contents/branches, pull requests, issues, and metadata. A GitHub App
can either supply an installation token through `GITHUB_TOKEN`, or let pg_hub
exchange one from:

```dotenv
GITHUB_APP_ID=123456
GITHUB_APP_INSTALLATION_ID=987654
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/pg-hub-app.pem
```

The App/PAT is used only against `GITHUB_TARGET_REPOSITORY`. Source requests are
anonymous GETs, and postgres.git is fetched read-only.

### GitHub Actions

The workflow in `.github/workflows/sync.yml` expects:

- repository variable `PG_HUB_TARGET_REPOSITORY`;
- optional variables `PG_HUB_BASE_BRANCH` and `PG_HUB_COMMITFEST_ID`;
- optional bootstrap bounds `PG_HUB_MAIL_LOOKBACK_MINUTES` and
  `PG_HUB_MAIL_MAX_MESSAGES` (the workflow defaults to 60 minutes / 50 mails);
- secret `PG_HUB_GITHUB_TOKEN` scoped to the target mirror.
- repository variable `PG_HUB_ENABLED=true` only after a manual fixture run
  succeeds; without it, scheduled jobs remain safely disabled.

It restores and saves `.pg_hub/state.db` through the Actions cache. For a real
deployment, a persistent volume and backed-up SQLite file are more predictable
than an Actions cache.

After adding the token, use **Actions → pg_hub read-only mirror → Run workflow**
and keep the default `fixture` source. This creates one controlled demonstration
PR in the mirror. Only after that succeeds should `PG_HUB_ENABLED=true` be set;
scheduled runs always use the live sources.

## Important demo limitations

- The official archive and CommitFest sites expose HTML rather than a versioned
  public API here. Their parsers are intentionally isolated and covered by
  contract-style tests, but markup changes will require adapter updates.
- Initial mail bootstrap is bounded by `PG_MAIL_LOOKBACK_MINUTES` and
  `PG_MAIL_MAX_MESSAGES`; this prevents accidentally opening hundreds of PRs.
- CommitFest entries are matched to known mail threads by explicit thread id in
  fixtures, stored patch mapping, or a conservative normalized-title match.
- Patch application tries `git am --3way`, then a plain indexed `git apply`.
  Conflicting or base-incompatible patchsets fail visibly and are retried; the
  previous mirror branch remains available.
- Patch branches use a depth-1 checkout of the configured GitHub mirror base;
  postgres.git commit polling keeps only a bounded recent history. The service
  never needs a full PostgreSQL history clone.
- Mirrored comments are authored by the bot and retain the real mail author in
  the comment body. Identity federation is out of scope for V0.

## Repository layout

```text
pg_hub/
  cli.py       commands and polling scheduler
  sources.py   pgsql-hackers, CommitFest, git, and fixture adapters
  sync.py      idempotent orchestration and rendering
  state.py     SQLite cursors, dedup records, and mappings
  github.py    GitHub PAT/App client and patch branch publisher
  labels.py    CommitFest and subsystem label rules
fixtures/      reproducible end-to-end demo data and patch versions
tests/         parser, safety, mapping, and idempotency checks
```

## Safety boundary

`pg_hub` is one-way by design:

```text
PostgreSQL sources ──GET/fetch──▶ pg_hub ──▶ your GitHub mirror
PostgreSQL sources ◀────────────── no write path ──────────────
```

Before moving beyond this demo, add fixture captures from several real threads,
pagination tests, rate-limit/backoff handling, observability, and a durable
deployment. Reverse email or CommitFest writes should remain a separate,
explicitly authorized phase.
