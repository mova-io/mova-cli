"""Fix registry — each fix is a self-contained :class:`Fix` instance.

The registry pattern keeps the dispatcher trivial (just iterate the
list) and makes adding a new fix a one-class change. Each fix
declares:

* its ``id`` (kebab-case, used in ``--only`` / ``--skip`` flags)
* a human-readable ``label`` and ``description``
* ``check(root)`` → ``True`` if the fix is *needed* (operators want
  to see "would apply" only for things that are actually broken)
* ``apply(root, *, dry_run)`` → :class:`FixResult` describing what
  happened (or would have happened in dry-run)

Fixes that fail mid-apply return ``FixStatus.FAILED`` with a
human-readable reason. The dispatcher continues with the next fix —
one bad permission shouldn't block creating ``.mdk/``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from movate.core.paths import LEGACY_STATE_DIR_NAME, STATE_DIR_NAME
from movate.credentials.loader import PROVIDER_KEY_ENV_VARS, _looks_like_runtime_key_env
from movate.credentials.store import CredentialsStore

# Canonical "tight" mode for secrets files. Lifted to a constant so the
# perms-check logic doesn't sprinkle 0o600 magic numbers all over.
_SECRETS_FILE_MODE = 0o600

# Shell profile files we scan for stale key exports. Order = display
# order in messages; we touch ALL that match (an export can be
# duplicated across several profiles).
_SHELL_PROFILE_NAMES: tuple[str, ...] = (".zshrc", ".bashrc", ".bash_profile", ".profile")

# Self-documenting marker prefixed onto any line we comment out, so the
# operator knows WHY it's disabled and our re-run comment-detection
# skips it (it starts with ``#``, so it's no longer an active export).
_DISABLED_MARKER = "# disabled by mdk fix (shadowed ~/.movate/credentials):"

# Suffix for the one-time backup we write before editing a profile.
_PROFILE_BACKUP_SUFFIX = ".mdk-bak"


class FixStatus(StrEnum):
    """Outcome of running one fix."""

    NOT_NEEDED = "not_needed"
    """The fix's check returned False — nothing to do."""

    WOULD_APPLY = "would_apply"
    """Dry-run mode + the fix is needed."""

    APPLIED = "applied"
    """Apply mode + the fix succeeded."""

    FAILED = "failed"
    """Apply attempted but raised — see :attr:`FixResult.message`."""


@dataclass(frozen=True)
class FixResult:
    """Outcome of dispatching one :class:`Fix`."""

    fix_id: str
    status: FixStatus
    message: str = ""
    """Operator-facing context — what was changed, or why it failed."""


@dataclass(frozen=True)
class Fix:
    """One auto-remediation. ``check`` + ``apply`` are pure-enough callables.

    Both callbacks take the project root. ``check`` returns whether the
    fix is needed; ``apply`` performs the change (or returns a no-op
    message in dry-run). Keeping the two separate makes the preview
    table fast even with many fixes — we only call ``apply`` when the
    operator opts in.
    """

    id: str
    label: str
    description: str
    check: Callable[[Path], bool]
    apply_fn: Callable[[Path, bool], FixResult]

    def run(self, project_root: Path, *, dry_run: bool) -> FixResult:
        """Dispatch: check first, short-circuit if not needed, else apply."""
        if not self.check(project_root):
            return FixResult(fix_id=self.id, status=FixStatus.NOT_NEEDED)
        try:
            return self.apply_fn(project_root, dry_run)
        except Exception as exc:
            return FixResult(
                fix_id=self.id,
                status=FixStatus.FAILED,
                message=str(exc),
            )


# ---------------------------------------------------------------------------
# Individual fix implementations
# ---------------------------------------------------------------------------


_GITIGNORE_BODY = """\
# movate runtime state — never commit
.mdk/local.db
.mdk/local.db-*

# Snapshots are commit-friendly by default; uncomment to opt out:
# .mdk/snapshots/

# Python
__pycache__/
*.pyc

# Editor / OS
.vscode/
.idea/
.DS_Store

# Secrets
.env
"""


def _check_movate_dir(root: Path) -> bool:
    """Fix needed when neither .mdk/ nor legacy .movate/ exists yet."""
    return not (root / STATE_DIR_NAME).is_dir() and not (root / LEGACY_STATE_DIR_NAME).is_dir()


def _apply_movate_dir(root: Path, dry_run: bool) -> FixResult:
    target = root / STATE_DIR_NAME
    if dry_run:
        return FixResult(
            fix_id="ensure-movate-dir",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target}",
        )
    target.mkdir(parents=True, exist_ok=True)
    return FixResult(
        fix_id="ensure-movate-dir",
        status=FixStatus.APPLIED,
        message=f"created {target}",
    )


def _check_gitignore(root: Path) -> bool:
    """Fix needed when .gitignore is absent. We DON'T overwrite an
    existing one — operators have their own conventions."""
    return not (root / ".gitignore").is_file()


def _apply_gitignore(root: Path, dry_run: bool) -> FixResult:
    target = root / ".gitignore"
    if dry_run:
        return FixResult(
            fix_id="ensure-gitignore",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target} with movate-aware ignores",
        )
    target.write_text(_GITIGNORE_BODY)
    return FixResult(
        fix_id="ensure-gitignore",
        status=FixStatus.APPLIED,
        message=f"created {target}",
    )


def _check_env_from_example(root: Path) -> bool:
    """Fix needed when .env.example exists but .env doesn't.

    We don't auto-create .env from scratch — without a template it'd
    be empty noise. Operators who don't have .env.example get a hint
    from doctor instead.
    """
    return (root / ".env.example").is_file() and not (root / ".env").is_file()


def _apply_env_from_example(root: Path, dry_run: bool) -> FixResult:
    example = root / ".env.example"
    target = root / ".env"
    if dry_run:
        return FixResult(
            fix_id="ensure-env-from-example",
            status=FixStatus.WOULD_APPLY,
            message=f"would copy {example} → {target} (you'll still need to fill in real values)",
        )
    target.write_text(example.read_text())
    return FixResult(
        fix_id="ensure-env-from-example",
        status=FixStatus.APPLIED,
        message=f"copied {example} → {target} — edit it to add real values",
    )


def _check_secrets_permissions(root: Path) -> bool:
    """Fix needed when ANY secrets file is world/group-readable.

    Walks ``~/.movate/secrets/*.yaml`` (the canonical secrets-store
    location). Returns True if any file is anything other than 0600.
    ``project_root`` is ignored — secrets are operator-wide, not
    project-wide, but we keep the signature consistent.
    """
    _ = root
    secrets_dir = Path(os.path.expanduser("~")) / ".movate" / "secrets"
    if not secrets_dir.is_dir():
        return False
    return any(
        (path.stat().st_mode & 0o777) != _SECRETS_FILE_MODE for path in secrets_dir.glob("*.yaml")
    )


def _apply_secrets_permissions(root: Path, dry_run: bool) -> FixResult:
    _ = root
    secrets_dir = Path(os.path.expanduser("~")) / ".movate" / "secrets"
    touched: list[str] = []
    for path in secrets_dir.glob("*.yaml"):
        mode = path.stat().st_mode & 0o777
        if mode == _SECRETS_FILE_MODE:
            continue
        touched.append(path.name)
        if not dry_run:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if dry_run:
        return FixResult(
            fix_id="fix-secrets-permissions",
            status=FixStatus.WOULD_APPLY,
            message=f"would chmod 0600 on {len(touched)} file(s): {', '.join(touched)}",
        )
    return FixResult(
        fix_id="fix-secrets-permissions",
        status=FixStatus.APPLIED,
        message=f"chmod 0600 on {len(touched)} file(s): {', '.join(touched)}",
    )


def _check_agents_dir(root: Path) -> bool:
    """Fix needed when agents/ is missing AND the project has movate.yaml.

    We only touch agents/ for actual movate projects — a stray ``mdk fix``
    run in a non-movate directory shouldn't sprinkle agents/ everywhere.
    """
    if not (root / "movate.yaml").is_file():
        return False
    return not (root / "agents").is_dir()


def _apply_agents_dir(root: Path, dry_run: bool) -> FixResult:
    target = root / "agents"
    gitkeep = target / ".gitkeep"
    if dry_run:
        return FixResult(
            fix_id="ensure-agents-dir",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target} (with .gitkeep)",
        )
    target.mkdir(exist_ok=True)
    gitkeep.write_text("")
    return FixResult(
        fix_id="ensure-agents-dir",
        status=FixStatus.APPLIED,
        message=f"created {target}/.gitkeep",
    )


# ---------------------------------------------------------------------------
# Shell-shadow fix: stale `export <VAR>=...` in a shell profile shadows a
# freshly-saved key in ~/.movate/credentials.
#
# Credential precedence is shell env > project .env > ~/.movate/credentials
# (see ``movate.credentials.loader.autoload_credentials``, which never
# clobbers an already-set env var). A leftover ``export MDK_DEV_KEY=...``
# (or ``export OPENAI_API_KEY=...``) in ~/.zshrc therefore SILENTLY wins
# over a rotated key in the credentials file → 401s the operator can't
# explain. The canonical home for these is ~/.movate/credentials, not the
# shell profile.
#
# A child process can't unset an env var in its parent shell, so the fix
# keys on the PERSISTENT, remediable source — the uncommented profile
# export line — and prints ``unset <VAR>`` as a manual follow-up for the
# live session.
# ---------------------------------------------------------------------------


def _tracked_shadow_vars() -> tuple[str, ...]:
    """Env vars whose presence in BOTH the credentials file and a shell
    profile constitutes a shadow.

    The runtime-bearer pattern ``MDK_<X>_KEY`` (matched by shape so we
    pick up every target the operator has saved, not a hardcoded list)
    plus the known LLM provider keys. We read the saved set from the
    credentials store so we only ever track vars the operator actually
    has a canonical value for — a benign lone export with no saved cred
    is never flagged.
    """
    saved = CredentialsStore().read()
    runtime_keys = tuple(name for name in saved if _looks_like_runtime_key_env(name))
    # De-dupe while preserving a stable order: provider keys first, then
    # any saved runtime-bearer keys.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in (*PROVIDER_KEY_ENV_VARS, *runtime_keys):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered)


def _profile_paths() -> list[Path]:
    """The user's shell profile files that currently exist.

    ``Path.home()`` honors ``$HOME`` (tests monkeypatch it) — we never
    hardcode an absolute home. Only existing files are returned; we
    don't create profiles that aren't there.
    """
    home = Path.home()
    return [home / name for name in _SHELL_PROFILE_NAMES if (home / name).is_file()]


def _active_export_pattern(var: str) -> re.Pattern[str]:
    """Match an *uncommented* ``export <VAR>=...`` or bare ``<VAR>=...`` line.

    Leading whitespace is tolerated; a leading ``#`` is NOT — already-
    commented lines (including ones we previously disabled) must not
    match, which is what makes the fix idempotent.
    """
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(var)}=")


def _profiles_shadowing(var: str) -> list[Path]:
    """Profile files containing at least one active export of ``var``."""
    pattern = _active_export_pattern(var)
    hits: list[Path] = []
    for path in _profile_paths():
        for raw in path.read_text().splitlines():
            if pattern.match(raw):
                hits.append(path)
                break
    return hits


def _check_unshadow_runtime_keys(root: Path) -> bool:
    """Fix needed when a tracked var is BOTH saved in ~/.movate/credentials
    AND actively exported (uncommented) in at least one shell profile.

    ``root`` is ignored — shell profiles are operator-wide, not project-
    wide — but we keep the signature consistent with every other fix.
    The dual condition (saved cred + active export) is deliberate: it
    flags a genuine shadow and never a benign lone export of a var the
    operator never saved.
    """
    _ = root
    return any(_profiles_shadowing(var) for var in _tracked_shadow_vars())


def _apply_unshadow_runtime_keys(root: Path, dry_run: bool) -> FixResult:
    """Comment out stale profile exports that shadow ~/.movate/credentials.

    Dry-run names each offending var + the profile file(s) and active
    line(s) that would be commented (VAR names only — never the secret
    value). Apply mode writes a one-time ``<profile>.mdk-bak`` before
    editing, then prefixes each matching active export with the
    self-documenting :data:`_DISABLED_MARKER` so it's inert + skipped on
    re-run. Idempotent: a second run finds no active export → NOT_NEEDED.
    """
    _ = root
    fix_id = "unshadow-runtime-keys"

    # Map of var -> profile paths that actively export it. Drives both
    # the dry-run message and the apply edits.
    shadows: dict[str, list[Path]] = {}
    for var in _tracked_shadow_vars():
        hits = _profiles_shadowing(var)
        if hits:
            shadows[var] = hits

    if dry_run:
        lines = [f"{var} in {', '.join(str(p) for p in paths)}" for var, paths in shadows.items()]
        return FixResult(
            fix_id=fix_id,
            status=FixStatus.WOULD_APPLY,
            message=(
                "would comment out shadowing export line(s): "
                + "; ".join(lines)
                + " (these shadow ~/.movate/credentials)"
            ),
        )

    # Apply: edit each affected profile once. A profile may shadow more
    # than one var, so collect the set of profiles + the vars each one
    # carries, edit it a single time, and back it up once.
    profiles_to_vars: dict[Path, list[str]] = {}
    for var, paths in shadows.items():
        for path in paths:
            profiles_to_vars.setdefault(path, []).append(var)

    commented: list[str] = []
    touched_vars: set[str] = set()
    for path, profile_vars in profiles_to_vars.items():
        _backup_profile_once(path)
        for var in _comment_active_exports(path, profile_vars):
            commented.append(f"{var} in {path}")
            touched_vars.add(var)

    follow_ups = " ".join(f"`unset {var}`" for var in sorted(touched_vars))
    return FixResult(
        fix_id=fix_id,
        status=FixStatus.APPLIED,
        message=(
            "commented out shadowing export line(s): "
            + "; ".join(commented)
            + ". Now run "
            + follow_ups
            + " in your current shell (or open a new shell) for the saved key to take effect."
        ),
    )


def _backup_profile_once(path: Path) -> None:
    """Write a one-time ``<profile>.mdk-bak`` before first edit.

    Skipped if a backup already exists — we never clobber an earlier
    backup (which would lose the pristine pre-mdk content).
    """
    backup = path.with_name(path.name + _PROFILE_BACKUP_SUFFIX)
    if backup.exists():
        return
    backup.write_text(path.read_text())


def _comment_active_exports(path: Path, vars_: list[str]) -> list[str]:
    """Comment out every active export of any var in ``vars_`` in ``path``.

    Returns the list of VAR names whose active export(s) were commented
    (names only — never values). Already-commented lines are skipped
    (their leading ``#`` means the active-export pattern won't match),
    keeping the operation idempotent.
    """
    patterns = {var: _active_export_pattern(var) for var in vars_}
    out: list[str] = []
    touched: list[str] = []
    for raw in path.read_text().splitlines():
        matched_var = next((var for var, pat in patterns.items() if pat.match(raw)), None)
        if matched_var is None:
            out.append(raw)
            continue
        out.append(f"{_DISABLED_MARKER} {raw}")
        touched.append(matched_var)
    path.write_text("\n".join(out) + "\n")
    return touched


# ---------------------------------------------------------------------------
# OCR optional-package fix helpers
# ---------------------------------------------------------------------------


def _uv_pip_install(packages: list[str], fix_id: str) -> FixResult:
    """Install ``packages`` into the current Python interpreter via uv pip.

    Works for both ``uv tool install``-ed mdk binaries (isolated venv in
    ``~/.local/share/uv/tools/movate-cli/``) and project-venv installs —
    ``--python sys.executable`` always targets the running interpreter's
    environment, bypassing any outer venv resolution.

    Raises on non-zero returncode so the caller's ``try/except`` in
    :meth:`Fix.run` converts it to ``FixStatus.FAILED``.
    """
    result = subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, *packages],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "uv pip install failed").strip())
    return FixResult(
        fix_id=fix_id,
        status=FixStatus.APPLIED,
        message=f"installed {', '.join(packages)}",
    )


def _check_ocr_extra(_root: Path) -> bool:
    """True when pdf2image or pytesseract is not importable."""
    return (
        importlib.util.find_spec("pdf2image") is None
        or importlib.util.find_spec("pytesseract") is None
    )


def _apply_ocr_extra(_root: Path, dry_run: bool) -> FixResult:
    packages = ["pdf2image", "pytesseract"]
    if dry_run:
        return FixResult(
            fix_id="install-ocr-extra",
            status=FixStatus.WOULD_APPLY,
            message=f"would install: {', '.join(packages)} (enables scanned PDF OCR via Tesseract)",
        )
    return _uv_pip_install(packages, "install-ocr-extra")


def _check_easyocr_extra(_root: Path) -> bool:
    """True when easyocr is not importable."""
    return importlib.util.find_spec("easyocr") is None


def _apply_easyocr_extra(_root: Path, dry_run: bool) -> FixResult:
    packages = ["easyocr"]
    if dry_run:
        return FixResult(
            fix_id="install-easyocr-extra",
            status=FixStatus.WOULD_APPLY,
            message=(
                "would install: easyocr (~300 MB, includes torch-cpu) — "
                "enables MOVATE_OCR_BACKEND=easyocr for noisy / low-quality scans"
            ),
        )
    return _uv_pip_install(packages, "install-easyocr-extra")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def available_fixes() -> list[Fix]:
    """The canonical fix list. Order = preview-display order.

    Ordering rationale: filesystem-only fixes first (movate dir, gitignore,
    env, agents dir), security-related fixes last (secrets perms) so
    they're visible / not buried.
    """
    return [
        Fix(
            id="ensure-movate-dir",
            label="Create .mdk/",
            description=(
                "Create the .mdk/ runtime directory. Houses local.db, "
                "snapshots/, promotions.yaml. Created lazily by most "
                "commands; this fix is for when ops want it eagerly."
            ),
            check=_check_movate_dir,
            apply_fn=_apply_movate_dir,
        ),
        Fix(
            id="ensure-gitignore",
            label="Create .gitignore",
            description=(
                "Create a movate-aware .gitignore with the standard "
                "ignores (.mdk/local.db, .env, __pycache__, etc.). "
                "Does NOT overwrite an existing .gitignore."
            ),
            check=_check_gitignore,
            apply_fn=_apply_gitignore,
        ),
        Fix(
            id="ensure-env-from-example",
            label="Create .env from .env.example",
            description=(
                "Copy .env.example to .env. You'll still need to fill "
                "in real API keys — this just establishes the template "
                "so dotenv loading works."
            ),
            check=_check_env_from_example,
            apply_fn=_apply_env_from_example,
        ),
        Fix(
            id="ensure-agents-dir",
            label="Create agents/ with .gitkeep",
            description=(
                "Create the empty agents/ directory if movate.yaml is "
                "present but agents/ isn't. Only fires in real movate "
                "projects — won't pollute non-movate directories."
            ),
            check=_check_agents_dir,
            apply_fn=_apply_agents_dir,
        ),
        Fix(
            id="fix-secrets-permissions",
            label="chmod 0600 on secrets files",
            description=(
                "Tighten ~/.movate/secrets/*.yaml to 0600 (user-only). "
                "These files contain plaintext API keys — wrong perms "
                "are a security incident waiting to happen."
            ),
            check=_check_secrets_permissions,
            apply_fn=_apply_secrets_permissions,
        ),
        Fix(
            id="unshadow-runtime-keys",
            label="Comment stale shell-profile key exports",
            description=(
                "Comment out a stale `export MDK_<X>_KEY=...` / "
                "`export OPENAI_API_KEY=...` (etc.) line in a shell profile "
                "(~/.zshrc, ~/.bashrc, ~/.bash_profile, ~/.profile) that "
                "shadows a freshly-saved key in ~/.movate/credentials, "
                "causing silent 401s. Fires only when the SAME var is both "
                "saved in the credentials file and actively exported in a "
                "profile. Writes a one-time <profile>.mdk-bak, then prints "
                "the `unset <VAR>` you must run in your current shell. Never "
                "prints or touches secret values; only edits profile files."
            ),
            check=_check_unshadow_runtime_keys,
            apply_fn=_apply_unshadow_runtime_keys,
        ),
        Fix(
            id="install-ocr-extra",
            label="Install OCR deps (pdf2image + pytesseract)",
            description=(
                "Install pdf2image and pytesseract into the active Python "
                "environment. Required for mdk kb ingest to OCR scanned / "
                "mixed PDFs via the default Tesseract backend. The Tesseract "
                "system binary (brew install tesseract) must also be on PATH."
            ),
            check=_check_ocr_extra,
            apply_fn=_apply_ocr_extra,
        ),
        Fix(
            id="install-easyocr-extra",
            label="Install EasyOCR (~300 MB)",
            description=(
                "Install easyocr (pure-Python, no system binary required). "
                "Enables MOVATE_OCR_BACKEND=easyocr — better accuracy on "
                "noisy or low-quality scans. Downloads ~300 MB of torch-cpu "
                "and model weights on first use. Skipped if already installed."
            ),
            check=_check_easyocr_extra,
            apply_fn=_apply_easyocr_extra,
        ),
    ]


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def diagnose_and_fix(
    project_root: Path,
    *,
    dry_run: bool = True,
    only: tuple[str, ...] = (),
    skip: tuple[str, ...] = (),
) -> list[FixResult]:
    """Run every applicable fix, returning per-fix results.

    ``only`` and ``skip`` filter the registry by fix id. ``only`` wins
    when both are supplied (operator's intent is the union of the
    declared subset, not the cross-product).
    """
    results: list[FixResult] = []
    for fix in available_fixes():
        if only and fix.id not in only:
            continue
        if skip and fix.id in skip:
            continue
        results.append(fix.run(project_root, dry_run=dry_run))
    return results
