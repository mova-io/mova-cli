"""Local-dev convenience: mint + seed a known dev API key for ``serve --dev``.

``mdk serve --dev`` collapses the three-step local loop —
``mdk auth mint`` → export ``MOVATE_SEED_API_KEY`` → paste the same key
into ``mdk playground serve`` — into a single flag. This module owns the
mint + insert, and emits the exact copy-paste playground command.

Scope of the convenience (read this before extending):

* It SEEDS a *valid* key into the runtime's storage. It does **not**
  disable auth or relax any auth dependency — every request still passes
  the same ``auth_dependency``; ``--dev`` only ensures a working key
  exists and tells you what it is.
* It is **local-dev only**. The caller (``serve``) refuses ``--dev`` on a
  non-loopback bind, so a known-shaped key is never seeded on a host that
  could be reached off-box. The warning printed alongside the key is part
  of that contract — keep it loud.
* The key is freshly minted on each boot (random secret, 256-bit
  entropy) — there is no hardcoded/static secret in the source. "Known"
  means *the operator is shown it on startup*, not *attacker-predictable*.

This is pure helper logic + a tiny optional file write. It parallels
``serve._seed_bootstrap_key``'s insert path rather than reusing it,
because the bootstrap seed derives its tenant/env from an externally
provided ``MOVATE_SEED_API_KEY`` and grants ``fleet-admin``; the dev key
is minted here with a fixed local tenant and a narrower
``read``/``run``/``admin`` grant.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from movate.core.auth import (
    SCOPE_ADMIN,
    SCOPE_KB_WRITE,
    SCOPE_READ,
    SCOPE_RUN,
    mint_api_key,
)
from movate.core.models import ApiKeyEnv
from movate.core.paths import project_state_dir

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

# Fixed local-dev tenant. Must be ≥ ``TENANT_PREFIX_LEN`` (8) chars so the
# minted key's tenant-prefix segment is well-defined. Deliberately obvious
# in `list-keys` / logs so a dev key is never mistaken for a real tenant.
DEV_TENANT_ID = "devtenant"

# Broad-but-not-fleet scopes: enough to list agents (read), run them (run),
# manage them (admin), and write to the KB / knowledge graph (kb:write — e.g.
# the ADR 079 graph-assert endpoint the playground demo calls) from the
# playground, without the cross-tenant reach of ``fleet-admin``. Least
# privilege for the local loop.
DEV_SCOPES = [SCOPE_READ, SCOPE_RUN, SCOPE_ADMIN, SCOPE_KB_WRITE]

# Local-dev key env: ``test`` (not ``live``) so a dev key is hard-separated
# from any real ``live`` key at parse time — a dev key can't authenticate
# against ``live`` infra even if it leaks into a shell history.
DEV_KEY_ENV = ApiKeyEnv.TEST

DEV_KEY_LABEL = "serve --dev (local)"

# Project-local file the dev key is optionally written to. NEVER the
# machine-global ``~/.movate/credentials`` — we must not risk clobbering a
# real key. A gitignored project-state file (``.mdk/``) or stdout only.
DEV_KEY_FILENAME = "dev-runtime-key"


async def mint_and_seed_dev_key(storage: StorageProvider) -> str:
    """Mint a fresh local-dev key, persist its record, and return the full key.

    Reuses :func:`movate.core.auth.mint_api_key` (random 256-bit secret) with
    the fixed local :data:`DEV_TENANT_ID` and the :data:`DEV_SCOPES` grant,
    then inserts the ``ApiKeyRecord`` via the storage Protocol — the same
    ``save_api_key`` path the bootstrap seed uses, so the key authenticates
    through the unchanged ``auth_dependency``.

    Returns the ``mvt_…`` full key string (shown once to the operator). The
    plaintext secret is never persisted — only the hash + salt on the record.
    """
    minted = mint_api_key(
        tenant_id=DEV_TENANT_ID,
        env=DEV_KEY_ENV,
        label=DEV_KEY_LABEL,
        scopes=DEV_SCOPES,
    )
    await storage.save_api_key(minted.record)
    return minted.full_key


def write_dev_key_file(full_key: str, *, root: Path | None = None) -> Path | None:
    """Best-effort write the dev key to ``<project-state-dir>/dev-runtime-key``.

    Returns the path written on success, or ``None`` if the write failed
    (e.g. a read-only checkout). Failure is non-fatal — the printed
    copy-paste command is the primary UX; the file is a convenience.

    Writes ONLY under the project-local ``.mdk/`` (resolved via
    :func:`movate.core.paths.project_state_dir`), never the machine-global
    ``~/.movate/`` credentials store, so a real key can't be clobbered. The
    file is created ``0600`` (best-effort) since it holds a live secret.
    """
    base = project_state_dir(root if root is not None else Path.cwd())
    target = base / DEV_KEY_FILENAME
    try:
        base.mkdir(parents=True, exist_ok=True)
        target.write_text(full_key + "\n", encoding="utf-8")
        # chmod can fail on some filesystems (e.g. Windows / mounted
        # volumes). The key write itself succeeded — don't treat a
        # permission-tightening miss as a hard failure.
        with contextlib.suppress(OSError):
            target.chmod(0o600)
        return target
    except OSError:
        return None


def playground_command(full_key: str, *, host: str, port: int) -> str:
    """Build the exact one-liner to launch the playground against this runtime.

    The playground reads its bearer from ``MOVATE_API_KEY`` (a.k.a.
    ``MDK_PLAYGROUND_API_KEY``) and its target from
    ``MDK_PLAYGROUND_RUNTIME_URL`` — see ``cli/playground.py``. Emitting
    those two env vars inline means the operator copies ONE line into a
    second terminal and the playground just connects.
    """
    runtime_url = f"http://{host}:{port}"
    return (
        f"MOVATE_API_KEY={full_key} MDK_PLAYGROUND_RUNTIME_URL={runtime_url} mdk playground serve"
    )


__all__ = [
    "DEV_KEY_ENV",
    "DEV_KEY_FILENAME",
    "DEV_KEY_LABEL",
    "DEV_SCOPES",
    "DEV_TENANT_ID",
    "mint_and_seed_dev_key",
    "playground_command",
    "write_dev_key_file",
]
