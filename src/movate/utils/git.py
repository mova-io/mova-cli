"""Git helpers for movate internals.

Thin wrappers around ``git`` subprocess calls that fail gracefully
when git is unavailable or the working directory is not a repo.
No gitpython dep — just shutil.which + subprocess.run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git_short_sha(cwd: Path | None = None) -> str:
    """Return the short git SHA of HEAD, or ``""`` when unavailable.

    Fails gracefully when:

    * ``git`` is not on PATH
    * the directory is not inside a git repo (git exits non-zero)
    * any subprocess / OS error occurs

    Parameters
    ----------
    cwd:
        Working directory for the git command. Defaults to the
        current process directory when ``None``. Pass
        ``project_root`` when you want the SHA of a specific repo
        (e.g. snapshot capture) rather than the process CWD.
    """
    if shutil.which("git") is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
