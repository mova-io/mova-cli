"""``mdk secrets`` — per-profile secret storage (Sprint O Day 4-7).

Distinct from ``mdk env`` (names + presence) — this owns **values**.

MVP scope:
* Local-only storage at ``~/.movate/secrets/<profile>.yaml``, file
  mode 0600 (user-only readable). Same convention as
  ``~/.aws/credentials``.
* Per-profile namespacing — the active profile (from
  :mod:`movate.profiles`) determines which file is read/written.
* CLI: ``mdk secrets {set, get, list, delete, export-shell}``.

What does NOT ship in MVP (deferred — follow-ups):
* Encryption at rest (OS keyring / operator-passphrase). File perms
  are the only line of defense today. The CLI prints a clear warning
  on every `set` so operators know what they're getting.
* Cloud sync (Azure Key Vault, AWS Secrets Manager). Local-only.
* SOPS integration. Operators who need git-tracked encrypted secrets
  use external SOPS today; native integration lands later.
* Secret rotation tracking (created_at + last_rotated). Plumbing
  in place; commands TBD.

The store interface is designed so the encryption + cloud-sync
follow-ups can drop in behind it without CLI changes.
"""

from __future__ import annotations

from movate.secrets.store import (
    Secret,
    SecretNotFoundError,
    SecretsStore,
    SecretsStoreError,
    load_store,
)

__all__ = [
    "Secret",
    "SecretNotFoundError",
    "SecretsStore",
    "SecretsStoreError",
    "load_store",
]
