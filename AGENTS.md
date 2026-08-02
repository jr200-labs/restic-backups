# Workspace Agent Rules

## Strict repository isolation

This reusable repository must remain entirely product-neutral. Never mention,
link to, depend on, inspect, or cross-reference downstream product repositories
in code, tests, fixtures, documentation, commits, issues, pull requests,
release notes, or GitHub metadata.

Dependency awareness is one-way: downstream applications may consume released
versions; this repository must not know which products consume it. Never create
downstream GitHub references to upstream issues or pull requests.

## Architecture

- Keep Python code under `src/restic_backups/`.
- Use the `restic-backups` CLI as the only credential and storage adapter.
- Keep YAML loading and validation in `config.py`. Keep SOPS handling,
  repository resolution, and restic execution under `restic_backups.generic`.
- Keep generic restic CLI definitions in `restic_backups.generic.cli`.
- Keep Voice Memos CLI definitions in `restic_backups.voice_memos.cli` and
  operational behavior in its pipeline/workflow modules.
- Preserve the configuration layers: `storage`, `restic-repositories`, and
  `backups` linked by `restic-repository-ids`. Storage uses the S3 or local
  backend; restic repositories may have an optional S3 `archive` policy.
- Backup sources may be absolute paths outside this repository. Managed local
  artifacts belong below `data/<storage-id>/<repository-path>/<job-id>/`.

## Safety

- Never print or commit decrypted SOPS values, credentials, recordings,
  transcripts, summaries, restic data, caches, or restores.
- Keep generated/private data ignored by the default-deny `.gitignore`.
- Do not contact remote storage during tests unless the user explicitly asks.
- Keep destructive pruning and cleanup explicitly confirmed.
- Preserve Full Disk Access guidance for macOS Voice Memos reads.

## Development

- Manage Python dependencies and commands with uv; development dependencies
  belong in the `dev` dependency group.
- Keep Quarto sources under `docs/` and generated `docs/_site/` ignored.
- Validate changes with the narrowest relevant commands, such as
  `uv run restic-backups check-config`, CLI help, and `make docs` for
  documentation changes.
