"""Machine-global provider credentials store.

Operators set provider API keys ONCE on a machine via
``mdk auth login <provider>``. The keys land in
``~/.movate/credentials`` (mode 0600), and every subsequent ``mdk``
invocation on that machine auto-loads them at startup. No more
``cp .env.example .env`` + paste in every new project.

Resolution order (narrowest beats widest):

1. Shell environment variable — CI / one-off overrides always win.
2. Per-project ``.env`` — project-specific keys (different customer
   Azure tenants, etc.).
3. Active-profile secrets (``mdk secrets set ...``) — per-environment
   keys (dev vs prod).
4. **``~/.movate/credentials``** — machine-global default (new).

The new file is the LOWEST precedence — it's the "default fallback"
every operator gets after one-time setup. Projects can still override.
CI can still override. But on a developer's laptop where they just
``mdk init`` somewhere new, the key Just Works.

Public surface:

* :class:`CredentialsStore` — read/write the file
* :func:`autoload_credentials` — called at every CLI startup
* :func:`verify_provider_key` — cheap test call to confirm a key works
* :data:`PROVIDER_KEY_ENV_VARS` — canonical list of provider env vars
"""

from __future__ import annotations

from movate.credentials.loader import (
    PROVIDER_KEY_ENV_VARS,
    autoload_credentials,
    key_source,
)
from movate.credentials.store import CREDENTIALS_PATH, CredentialsStore
from movate.credentials.verify import (
    VerifyResult,
    verify_provider_key,
)

__all__ = [
    "CREDENTIALS_PATH",
    "PROVIDER_KEY_ENV_VARS",
    "CredentialsStore",
    "VerifyResult",
    "autoload_credentials",
    "key_source",
    "verify_provider_key",
]
