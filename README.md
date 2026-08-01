# Restic Backups

YAML configuration and a Python CLI for running multiple restic repositories,
with optional SOPS decryption. The package currently includes a complete macOS
Voice Memos workflow for backup, transcription, summarisation, and speaker
diarization.

Backup payloads, generated metadata, model caches, and restores are excluded
from Git by a default-deny `.gitignore`.

## Install

```sh
make install-deps  # Homebrew: restic, sops, uv, ffmpeg, jq, coreutils
make install       # uv sync, including the dev group and Quarto
```

`make init` loads the selected configuration and initializes each enabled store
that does not already exist. It reports and skips disabled or initialized
stores, and does not back up files.

## CLI

Use the built-in help for available commands and options:

```sh
uv run restic-backups --help
uv run restic-backups generic --help
uv run restic-backups voice-memos --help
```

## Configuration

Pass a plain YAML file explicitly:

```sh
uv run restic-backups --config config.yaml check-config
```

For SOPS, add `--sops`. The equivalent environment variables are
`RESTIC_BACKUPS_CONFIG` and `RESTIC_BACKUPS_SOPS=1`; they also configure
`make config-check` and `make init`.

The configuration separates:

- `credentials`: reusable S3-compatible authentication;
- `restic-stores`: endpoint, region, bucket, key prefix/password, and optional
  archive policy;
- `backups`: CLI selections linked to a store, local source paths, and an
  optional restic snapshot tag (defaulting to the backup ID).

One credential may serve many stores, and multiple backups may share a store.
Disabled stores may contain `CHANGE_ME`; all placeholders must be replaced
before enabling one.

## Data and source paths

Managed local artifacts use:

```text
data/<store-id>/<bucket>/<key-prefix>/<backup-id>/
```

This directory is created beside the selected configuration file. It is
metadata/workspace organization, not a restriction on backup sources. Restic
may back up absolute paths anywhere on the machine. Resolve a managed directory
without exposing credentials with:

```sh
uv run restic-backups generic data-dir voice-memos
```

## AWS Glacier

Use `GLACIER_IR` with `restore: null` for normal immediate restic access. Cold
`GLACIER` and `DEEP_ARCHIVE` stores require a configured retrieval tier, days,
and timeout. Retrieval must also be acknowledged at runtime:

```sh
ALLOW_ARCHIVE_RETRIEVAL=1 uv run restic-backups generic run \
  --backup <backup-id> restore latest --target <dir>
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
