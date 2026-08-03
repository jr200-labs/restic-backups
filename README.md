# Restic Backups

YAML configuration and a Python CLI for running multiple restic repositories,
with optional SOPS decryption. The package currently includes a complete macOS
Voice Memos workflow for backup, transcription, summarisation, and speaker
diarization, plus managed backups of explicit GitHub repositories or every
repository owned by a GitHub organization or user.

## Why

Create encrypted, deduplicated **incremental backups** that upload only new or
changed data. This makes reliable off-site backups practical with
[cheaper storage options](docs/storage-costs.qmd), without maintaining a
separate backup script for every repository.

Backup payloads, generated metadata, model caches, and restores are excluded
from Git by a default-deny `.gitignore`.

## Install

```sh
make install-deps  # Homebrew tools, including restic, sops, git-lfs, gh, and uv
make install       # uv sync, including the dev group and Quarto
```

`make init` loads the selected configuration and initializes each enabled restic
repository that does not already exist. It reports and skips disabled or
initialized repositories, and does not back up files.

## CLI

Use the built-in help for available commands and options:

```sh
uv run restic-backups  # interactive arrow-key menu
uv run restic-backups --help
uv run restic-backups job --help
uv run restic-backups github-repository --help
uv run restic-backups generic --help
uv run restic-backups voice-memos --help
```

The interactive menus include **Help** and **Back** at every level. Press
Escape to go back or Ctrl+C to exit. Help stays at the current level and does
not load configuration or access a repository.
Generic write actions also offer a Space-toggleable **Dry run** checkbox so the
operation can be inspected without changing repository data.

Command auditing is enabled by default and appends JSON records to
`audit-log.json` in the current directory. Set `RESTIC_BACKUPS_AUDIT=0` to
disable it. Passwords and other secret-like argument values are redacted.
GitHub jobs also audit their raw `git`, `git lfs`, `gh`, and `restic` commands;
tokens and temporary credential paths remain outside those arguments.

Logs use standard Python logging with timestamps. To publish job result and
duration metrics, set `RESTIC_BACKUPS_PROMETHEUS_PUSHGATEWAY_URL` to a
Prometheus Pushgateway URL. See the CLI documentation for the metric names.

## Configuration

Pass a plain YAML file explicitly:

```sh
uv run restic-backups --config config.yaml check-config
```

For SOPS, add `--sops`. The equivalent environment variables are
`RESTIC_BACKUPS_CONFIG` and `RESTIC_BACKUPS_SOPS=1`; they also configure
`make config-check` and `make init`.

The configuration separates:

- `storage`: S3-compatible services and mounted local filesystems, with S3
  credentials kept on the relevant storage entry;
- `restic-repositories`: encrypted repositories within storage, including the
  bucket/key prefix or local path, restic password, cache, and archive policy;
- `jobs`: typed work linked to one or more restic repositories. `files`,
  `github-repository`, `github-owner`, and `voice-memos` jobs share the same
  destination and snapshot fields while defining their input under `source`.
One `github-repository` job may incrementally maintain multiple repository URLs
and snapshot their combined state together.
One `github-owner` job discovers every repository visible to the active GitHub
credentials before using that same multi-repository workflow. Local runs reuse
`gh auth login`; unattended runs may read a token from an environment variable
or mounted file. A dry run still performs read-only discovery so it can report
the exact plan, but never runs Git or writes to restic.

Multiple restic repositories may use one storage backend, one job may
write to several repositories, and several jobs may share one repository. The
job TUI preselects a sole enabled destination; multiple destinations start
unchecked. Disabled repositories may contain `CHANGE_ME`; all placeholders must
be replaced before enabling one.

## Data and source paths

Managed local artifacts use:

```text
data/<storage-id>/<repository-path>/<job-id>/
```

This directory is created beside the selected configuration file. It is
metadata/workspace organization, not a restriction on backup sources. Restic
may back up absolute paths anywhere on the machine. List or run every job type
through the same commands:

```sh
uv run restic-backups job list
uv run restic-backups job run documents
```

## AWS Glacier

Use `GLACIER_IR` with `restore: null` for normal immediate restic access. Cold
`GLACIER` and `DEEP_ARCHIVE` repositories require a configured retrieval tier,
days, and timeout. Retrieval must also be acknowledged at runtime:

```sh
ALLOW_ARCHIVE_RETRIEVAL=1 uv run restic-backups generic restic run \
  --backup <job-id> restore latest --target <dir>
```

Storage-class changes apply only to new objects. Use a new `key_prefix` instead
of mixing storage policies in one repository.

## Documentation

```sh
make docs          # render docs/_site
make docs-preview  # local preview server
```

Start with the [Quick Start](docs/quick-start.qmd). Never commit decrypted SOPS
configuration or anything below `data/`.

For complete reachable Git history, multiple explicit repository URLs, owner
discovery, and optional LFS objects, wikis, GitHub metadata, and release assets,
see [GitHub Backups](docs/github-repositories.qmd).
