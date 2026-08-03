"""Maintain a local GitHub repository export and snapshot it with restic."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from .. import audit, config
from ..errors import BackupError
from ..generic import restic

USABLE = {"updated", "unchanged", "stale"}
logger = logging.getLogger(__name__)


def fail(message: str) -> NoReturn:
    raise BackupError(message)


def data_dir(job_id: str) -> Path:
    if not job_id or job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
        fail("unsafe GitHub backup job ID")
    return (
        config.config_path().resolve().parent / "data" / "github-repositories" / job_id
    )


@contextmanager
def authentication(
    github: Mapping[str, Any], *, git: bool = True, api: bool = True
) -> Iterator[dict[str, str]]:
    """Build subprocess authentication without putting secrets in arguments."""
    env = os.environ.copy()
    auth = github.get("authentication", {})
    git_auth = auth.get("git", {})
    with tempfile.TemporaryDirectory(prefix="restic-backups-github-auth-") as temporary:
        directory = Path(temporary)
        if git:
            env["GIT_TERMINAL_PROMPT"] = "0"
        if git and "ssh" in git_auth:
            ssh = git_auth["ssh"]
            key = directory / "private-key"
            hosts = directory / "known-hosts"
            key.write_text(
                config.credential_value(ssh["private-key"], "Git SSH private key")
            )
            hosts.write_text(
                config.credential_value(ssh["known-hosts"], "Git SSH known-hosts")
            )
            key.chmod(stat.S_IRUSR | stat.S_IWUSR)
            hosts.chmod(stat.S_IRUSR | stat.S_IWUSR)
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={hosts} "
                "-o StrictHostKeyChecking=yes"
            )
        if git and "https" in git_auth:
            askpass = directory / "askpass"
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token;; "
                "*) printf '%s\\n' \"$GIT_AUTH_TOKEN\";; esac\n"
            )
            askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env.update(
                GIT_ASKPASS=str(askpass),
                GIT_AUTH_TOKEN=config.credential_value(
                    git_auth["https"]["token"], "Git HTTPS token"
                ).strip(),
            )
        if api and "api" in auth:
            env["GH_TOKEN"] = config.credential_value(
                auth["api"]["token"], "GitHub API token"
            ).strip()
        yield env


def _run(
    args: list[str],
    *,
    env: Mapping[str, str],
    output: BinaryIO | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            env=env,
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=output is None,
            check=False,
        )
    except FileNotFoundError:
        fail(f"{args[0]} is not installed")
    if check and result.returncode:
        message = f"{' '.join(args[:2])} failed with exit code {result.returncode}"
        if result.stderr:
            message += f": {audit.redact_args([result.stderr.strip()])[0]}"
        fail(message)
    return result  # type: ignore[return-value]


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _replace_directory(staging: Path, destination: Path) -> None:
    previous = destination.with_name(f".{destination.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if destination.exists():
        destination.replace(previous)
    try:
        staging.replace(destination)
    except OSError:
        if previous.exists():
            previous.replace(destination)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def _git_mirror(url: str, destination: Path, env: Mapping[str, str]) -> str:
    common = ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
    if destination.exists():
        _run(
            ["git", "-C", str(destination), *common, "remote", "update", "--prune"],
            env=env,
        )
        return "updated"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", *common, "clone", "--mirror", url, str(destination)], env=env)
    except (BackupError, OSError):
        if destination.exists():
            shutil.rmtree(destination)
        raise
    _run(["git", "-C", str(destination), "config", "gc.auto", "0"], env=env)
    _run(
        ["git", "-C", str(destination), "config", "maintenance.auto", "false"],
        env=env,
    )
    return "updated"


def _wiki_url(repository_url: str, owner: str, name: str) -> str:
    if repository_url.startswith("git@"):
        return f"git@github.com:{owner}/{name}.wiki.git"
    return f"https://github.com/{owner}/{name}.wiki.git"


def _wiki(
    repository_url: str, owner: str, name: str, path: Path, env: Mapping[str, str]
) -> str:
    if path.exists():
        return _git_mirror(_wiki_url(repository_url, owner, name), path, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "git",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        "clone",
        "--mirror",
        _wiki_url(repository_url, owner, name),
        str(path),
    ]
    result = _run(args, env=env, check=False)
    if result.returncode:
        if path.exists():
            shutil.rmtree(path)
        if "repository not found" in result.stderr.lower():
            return "not-present"
        fail(f"git clone failed with exit code {result.returncode}")
    _run(["git", "-C", str(path), "config", "gc.auto", "0"], env=env)
    _run(["git", "-C", str(path), "config", "maintenance.auto", "false"], env=env)
    return "updated"


def _lfs(repository: Path, env: Mapping[str, str]) -> str:
    _run(["git", "-C", str(repository), "lfs", "fetch", "--all", "origin"], env=env)
    return "updated"


def _gh_json(args: list[str], env: Mapping[str, str]) -> Any:
    result = _run(["gh", "api", *args], env=env)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("GitHub returned invalid JSON")


def owner_repository_urls(github: Mapping[str, Any]) -> list[str]:
    """Enumerate repositories visible to the configured GitHub API token."""
    owner = config.github_owner_name(github["owner-url"], "owner-url")
    with authentication(github, git=False) as env:
        account = _gh_json([f"users/{owner}"], env)
        if account.get("type") == "Organization":
            endpoint = f"orgs/{owner}/repos?per_page=100&type=all&sort=full_name"
        else:
            viewer = _gh_json(["user"], env)
            endpoint = (
                "user/repos?per_page=100&affiliation=owner&visibility=all&sort=full_name"
                if str(viewer.get("login", "")).lower() == owner.lower()
                else f"users/{owner}/repos?per_page=100&type=owner&sort=full_name"
            )
        pages = _gh_json(["--paginate", "--slurp", endpoint], env)
    field = "ssh_url" if github["clone-protocol"] == "ssh" else "clone_url"
    try:
        repositories = [item for page in pages for item in page]
        urls = [
            item[field]
            for item in sorted(
                repositories, key=lambda item: str(item["full_name"]).lower()
            )
        ]
        if any(not isinstance(url, str) or not url for url in urls):
            raise TypeError
        for index, url in enumerate(urls):
            config.github_repository_name(url, f"enumerated repository {index}")
        return urls
    except (config.ConfigError, KeyError, TypeError):
        fail("GitHub repository enumeration returned invalid data")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    root = destination.resolve()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root) or member.issym() or member.islnk():
                fail("GitHub migration archive contains an unsafe path")
            if not (member.isfile() or member.isdir()):
                fail("GitHub migration archive contains an unsupported entry")
        source.extractall(destination, members=members)


def _metadata(
    owner: str,
    name: str,
    destination: Path,
    timeout: int,
    env: Mapping[str, str],
) -> str:
    repository = _gh_json([f"repos/{owner}/{name}"], env)
    is_org = repository.get("owner", {}).get("type") == "Organization"
    endpoint = f"orgs/{owner}/migrations" if is_org else "user/migrations"
    started = _gh_json(
        [
            "--method",
            "POST",
            endpoint,
            "-F",
            f"repositories[]={owner}/{name}",
            "-F",
            "lock_repositories=false",
            "-F",
            "exclude_metadata=false",
            "-F",
            "exclude_git_data=true",
            "-F",
            "exclude_attachments=false",
            "-F",
            "exclude_releases=false",
            "-F",
            "exclude_owner_projects=true",
        ],
        env,
    )
    migration_id = started.get("id")
    if not isinstance(migration_id, int):
        fail("GitHub migration response did not include an ID")
    item = (
        f"orgs/{owner}/migrations/{migration_id}"
        if is_org
        else f"user/migrations/{migration_id}"
    )
    deadline = time.monotonic() + timeout
    while True:
        state = _gh_json([item], env).get("state")
        logger.debug("GitHub migration %s: %s", migration_id, state)
        if state == "exported":
            break
        if state in {"failed", "failed_validation"}:
            fail(f"GitHub migration ended in state {state}")
        if time.monotonic() >= deadline:
            fail(f"GitHub migration did not finish within {timeout} seconds")
        time.sleep(min(5, max(0, deadline - time.monotonic())))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging_root = Path(temporary)
        archive = staging_root / "migration.tar.gz"
        extracted = staging_root / "extracted"
        with archive.open("wb") as output:
            _run(["gh", "api", f"{item}/archive"], env=env, output=output)
        _safe_extract(archive, extracted)
        _replace_directory(extracted, destination)
    _run(["gh", "api", "--method", "DELETE", f"{item}/archive"], env=env, check=False)
    return "updated"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "asset"


def _releases(owner: str, name: str, destination: Path, env: Mapping[str, str]) -> str:
    pages = _gh_json(["--paginate", "--slurp", f"repos/{owner}/{name}/releases"], env)
    releases = [item for page in pages for item in page]
    previous_path = destination / "releases.json"
    try:
        previous = json.loads(previous_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {"assets": {}}
    previous_assets = previous.get("assets", {})
    current: dict[str, dict[str, Any]] = {}
    destination.mkdir(parents=True, exist_ok=True)
    for release in releases:
        for asset in release.get("assets", []):
            asset_id = str(asset["id"])
            relative = f"{release['id']}/{asset_id}-{_safe_name(asset['name'])}"
            target = destination / relative
            details = {
                "path": relative,
                "name": asset["name"],
                "size": asset["size"],
                "updated_at": asset["updated_at"],
                "release_id": release["id"],
            }
            current[asset_id] = details
            if previous_assets.get(asset_id) == details and target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp")
            with temporary.open("wb") as output:
                _run(
                    [
                        "gh",
                        "api",
                        "-H",
                        "Accept: application/octet-stream",
                        f"repos/{owner}/{name}/releases/assets/{asset_id}",
                    ],
                    env=env,
                    output=output,
                )
            temporary.replace(target)
    for asset_id, details in previous_assets.items():
        if asset_id not in current:
            (destination / details["path"]).unlink(missing_ok=True)
    _atomic_json(previous_path, {"repository": f"{owner}/{name}", "assets": current})
    return "updated" if current != previous_assets else "unchanged"


def _component_path(root: Path, component: str) -> Path:
    return {
        "git": root / "repository.git",
        "lfs": root / "repository.git" / "lfs",
        "wiki": root / "wiki.git",
        "metadata": root / "github-export",
        "release-assets": root / "release-assets",
    }[component]


def _update_repository(
    job_id: str,
    github: Mapping[str, Any],
    repository_url: str,
    owner: str,
    name: str,
    root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    statuses: dict[str, str] = {}
    errors: dict[str, str] = {}
    label = f"{owner}/{name}"
    for component in ("git", "lfs", "wiki", "metadata", "release-assets"):
        if not github["components"][component]:
            statuses[component] = "disabled"
            continue
        path = _component_path(root, component)
        existed = path.exists()
        logger.info("%s: %s: updating %s", job_id, label, component)
        try:
            with authentication(
                github,
                git=component in {"git", "lfs", "wiki"},
                api=component in {"metadata", "release-assets"},
            ) as env:
                if component == "git":
                    statuses[component] = _git_mirror(repository_url, path, env)
                elif component == "lfs":
                    statuses[component] = _lfs(root / "repository.git", env)
                elif component == "wiki":
                    statuses[component] = _wiki(repository_url, owner, name, path, env)
                elif component == "metadata":
                    statuses[component] = _metadata(
                        owner,
                        name,
                        path,
                        github["migration-timeout-seconds"],
                        env,
                    )
                else:
                    statuses[component] = _releases(owner, name, path, env)
        except (
            BackupError,
            OSError,
            tarfile.TarError,
            KeyError,
            ValueError,
        ) as exc:
            statuses[component] = "stale" if existed else "failed"
            errors[component] = str(exc)
    return statuses, errors


def _preflight_metadata(
    github: Mapping[str, Any], repositories: list[tuple[str, str, str]]
) -> None:
    if not github["components"]["metadata"]:
        return
    checked: set[str] = set()
    with authentication(github, git=False) as env:
        for _, owner, name in repositories:
            details = _gh_json([f"repos/{owner}/{name}"], env)
            endpoint = (
                f"orgs/{owner}/migrations"
                if details.get("owner", {}).get("type") == "Organization"
                else "user/migrations"
            )
            if endpoint not in checked:
                _gh_json([f"{endpoint}?per_page=1"], env)
                checked.add(endpoint)


def _preflight_destinations(
    job_id: str,
    selected_repositories: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
) -> None:
    for repository_id in selected_repositories:
        code = restic.command(
            job_id,
            ["cat", "config"],
            storage,
            repositories,
            jobs,
            repository_id=repository_id,
        )
        if code:
            fail(f"{repository_id}: repository check failed with exit code {code}")


def backup(
    job_id: str,
    backup_config: Mapping[str, Any],
    selected_repositories: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[dict[str, str], dict[str, bool]]:
    github = backup_config["source"]
    parsed = [
        (
            url,
            *config.github_repository_name(
                url, f"{job_id}.source.repository-urls[{index}]"
            )[:2],
        )
        for index, url in enumerate(github["repository-urls"])
    ]
    if dry_run:
        return (
            {
                f"{owner}/{name}:{component}": (
                    "disabled" if not enabled else "planned"
                )
                for _, owner, name in parsed
                for component, enabled in github["components"].items()
            },
            {repository_id: True for repository_id in selected_repositories},
        )

    _preflight_destinations(
        job_id, selected_repositories, storage, repositories, backups
    )
    _preflight_metadata(github, parsed)

    root = data_dir(job_id)
    root.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, str] = {}
    manifest_repositories: dict[str, Any] = {}
    paths: list[str] = []
    excludes: list[str] = []
    for repository_url, owner, name in parsed:
        label = f"{owner}/{name}"
        repository_root = root / owner / name
        repository_statuses, errors = _update_repository(
            job_id, github, repository_url, owner, name, repository_root
        )
        statuses.update(
            {
                f"{label}:{component}": status
                for component, status in repository_statuses.items()
            }
        )
        manifest_repositories[label] = {
            "source": repository_url,
            "components": {
                component: {
                    "status": status,
                    **({"error": errors[component]} if component in errors else {}),
                }
                for component, status in repository_statuses.items()
            },
        }
        for component in ("git", "wiki", "metadata", "release-assets"):
            path = _component_path(repository_root, component)
            if repository_statuses.get(component) in USABLE and path.exists():
                paths.append(str(path))
        if not github["components"]["lfs"]:
            excludes.extend(
                ["--exclude", str(repository_root / "repository.git" / "lfs")]
            )

    manifest = {
        "job-id": job_id,
        "sources": github["repository-urls"],
        **({"owner": github["owner-url"]} if "owner-url" in github else {}),
        "updated-at": datetime.now(UTC).isoformat(),
        "repositories": manifest_repositories,
    }
    manifest_path = root / "backup-manifest.json"
    _atomic_json(manifest_path, manifest)
    args = ["backup", *excludes, str(manifest_path), *paths]
    destinations: dict[str, bool] = {}
    for repository_id in selected_repositories:
        logger.info("%s: snapshotting to %s", job_id, repository_id)
        try:
            destinations[repository_id] = (
                restic.command(
                    job_id,
                    args,
                    storage,
                    repositories,
                    backups,
                    repository_id=repository_id,
                )
                == 0
            )
        except (BackupError, OSError):
            destinations[repository_id] = False
    return statuses, destinations


def backup_owner(
    job_id: str,
    backup_config: Mapping[str, Any],
    selected_repositories: list[str],
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    backups: dict[str, dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[dict[str, str], dict[str, bool]]:
    """Enumerate one GitHub owner and run the shared repository workflow."""
    github = dict(backup_config["source"])
    urls = owner_repository_urls(github)
    if not urls:
        fail(f"no repositories are visible for {github['owner-url']}")
    logger.info("%s: discovered %d GitHub repositories", job_id, len(urls))
    github["repository-urls"] = urls
    expanded = dict(backup_config)
    expanded["source"] = github
    return backup(
        job_id,
        expanded,
        selected_repositories,
        storage,
        repositories,
        backups,
        dry_run=dry_run,
    )


def read_manifest(job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((data_dir(job_id) / "backup-manifest.json").read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        fail(f"backup manifest for '{job_id}' is invalid")


def manifest_components(
    manifest: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Return labeled component results."""
    return [
        (f"{repository}:{component}", result)
        for repository, details in manifest.get("repositories", {}).items()
        for component, result in details.get("components", {}).items()
    ]


def snapshot_repositories(job_id: str, snapshot: Mapping[str, Any]) -> dict[str, str]:
    """Return GitHub repository labels and saved bare-repository paths."""
    paths = snapshot.get("paths")
    if not isinstance(paths, list):
        fail("restic snapshot did not contain a path list")
    found: dict[str, str] = {}
    for value in paths:
        if not isinstance(value, str):
            fail("restic snapshot contained an invalid path")
        parts = Path(value).parts
        if len(parts) >= 4 and parts[-4] == job_id and parts[-1] == "repository.git":
            found[f"{parts[-3]}/{parts[-2]}"] = value
    if not found:
        fail(f"snapshot does not contain Git repositories for job '{job_id}'")
    return dict(sorted(found.items()))


def restore_repository(
    job_id: str,
    snapshot_id: str,
    snapshot_path: str,
    target: Path,
    mode: str,
    repository_id: str,
    storage: dict[str, dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
) -> None:
    """Restore one backed-up Git mirror as a bare repository or working clone."""
    target = target.expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        fail(f"restore target must be absent or an empty directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    def restore_bare(destination: Path) -> None:
        code = restic.command(
            job_id,
            [
                "restore",
                f"{snapshot_id}:{snapshot_path}",
                "--target",
                str(destination),
                "--verify",
            ],
            storage,
            repositories,
            jobs,
            repository_id=repository_id,
        )
        if code:
            fail(f"restic restore failed with exit code {code}")

    if mode == "bare":
        restore_bare(target)
        return
    if mode != "clone":
        fail("restore mode must be 'bare' or 'clone'")

    with tempfile.TemporaryDirectory(prefix="restic-backups-github-restore-") as tmp:
        bare = Path(tmp) / "repository.git"
        restore_bare(bare)
        env = os.environ.copy()
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        try:
            origin = _run(
                ["git", "--git-dir", str(bare), "config", "--get", "remote.origin.url"],
                env=env,
                check=False,
            )
            _run(
                ["git", "clone", "--no-hardlinks", str(bare), str(target)],
                env=env,
            )
            if origin.returncode == 0 and origin.stdout.strip():
                _run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "remote",
                        "set-url",
                        "origin",
                        origin.stdout.strip(),
                    ],
                    env=env,
                )
            else:
                _run(
                    ["git", "-C", str(target), "remote", "remove", "origin"],
                    env=env,
                )
            if (bare / "lfs").is_dir():
                shutil.copytree(
                    bare / "lfs", target / ".git" / "lfs", dirs_exist_ok=True
                )
                _run(["git", "-C", str(target), "lfs", "checkout"], env=env)
        except (BackupError, OSError):
            if target.exists():
                shutil.rmtree(target)
            raise
