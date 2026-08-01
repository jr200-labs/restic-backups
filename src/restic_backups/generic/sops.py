"""Decrypt SOPS configuration files."""

import json
import subprocess
from pathlib import Path
from typing import Any

from ..errors import BackupError

SOPS_ENV = "RESTIC_BACKUPS_SOPS"


def decrypt(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["sops", "--decrypt", "--output-type", "json", path],
            check=True,
            capture_output=True,
            text=True,
        )
        config = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise BackupError("sops is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupError(exc.stderr.strip() or f"could not decrypt {path}") from exc
    except json.JSONDecodeError as exc:
        raise BackupError(f"decrypted config is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise BackupError(f"config in {path} must be a mapping")
    return config
