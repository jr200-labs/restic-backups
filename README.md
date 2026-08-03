# Restic Backups

Config-driven **incremental** Restic backups with built-in SOPS support.

[🚀 Quick Start](https://jr200-labs.github.io/restic-backups/quick-start.html) ·
[📚 Documentation](https://jr200-labs.github.io/restic-backups/)

## Why Restic Backups?

- 💸 **Reduce costs** by sending deduplicated backups to lower-cost S3,
  S3-compatible, Glacier, or local storage. See the
  [storage cost comparison](https://jr200-labs.github.io/restic-backups/storage-costs.html).
- 🐙 Back up GitHub at organization or individual repository level,
  including full Git history and optional LFS, metadata, wikis, and releases.
- 🎙️ Back up iOS Voice Memos on macOS, with optional transcription,
  summarisation, and speaker diarization.
- 🔐 Keep credentials and repository passwords encrypted in a SOPS-managed YAML
  configuration.

## Install

```sh
make install-deps  # Homebrew tools, including restic, sops, git-lfs, gh, and uv
make install       # uv sync, including the dev group and Quarto
```

`make init` loads the selected configuration and initializes each enabled restic
repository that does not already exist. It reports and skips disabled or
initialized repositories, and does not back up files.

## Run backups

Run without arguments for a friendly interactive TUI. Navigate with the arrow
keys, select destinations with Space, and use the described menus to manage
jobs, repositories, and snapshots:

```sh
uv run restic-backups
```

For cron, containers, and other batch jobs, use the same operations as explicit
CLI commands:

```sh
uv run restic-backups job run documents --repository personal-b2
uv run restic-backups --help
```

See the [CLI guide](https://jr200-labs.github.io/restic-backups/cli.html) for
commands, dry runs, audit logs, timestamped logging, and Prometheus metrics.

## Configuration

Pass a plain YAML file explicitly:

```sh
uv run restic-backups --config config.yaml check-config
```

For SOPS, add `--sops`. The equivalent environment variables are
`RESTIC_BACKUPS_CONFIG` and `RESTIC_BACKUPS_SOPS=1`; they also configure
`make config-check` and `make init`.

The configuration separates:

- `storage`: enabled or disabled S3-compatible services and mounted local
  filesystems, with S3 credentials kept on the relevant storage entry;
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
job TUI preselects a sole available destination; multiple destinations start
unchecked. Disabling storage leaves its repositories and jobs visible but
unselectable. Disabled storage and repositories may contain `CHANGE_ME`; all
placeholders must be replaced before enabling them.

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
