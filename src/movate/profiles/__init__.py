"""``mdk profiles`` — named environment contexts (Sprint O Day 1-3).

Layer on top of the existing ``mdk config`` target registry:

* ``mdk config`` manages individual *targets* — the URL + API key
  pairs that connect to a deployed runtime. Stays as-is.
* ``mdk profiles`` is the operator-friendly **named context** that
  bundles a target reference, a tenant_id, an operator-supplied
  description, and (future) a secrets namespace, default policy
  overrides, etc.

Why layer rather than replace: targets are a *technical* concern
(how do I connect to that runtime?) — profiles are an *operational*
concern (what is "dev" vs "staging" vs "prod" to me as the
operator?). The two abstractions live at different altitudes.

Active-profile concept matches kubectl context / aws profile /
gcloud config — one currently-selected profile per shell session,
overridable per-command. The active profile's selection lives at
``~/.movate/active-profile`` (a one-line text file); the registry
itself lives at ``~/.movate/profiles.yaml``.

Future Sprint O integrations (per-profile namespacing, all behind
this same registry):
* ``mdk secrets`` — secrets namespaced per profile
* ``mdk env`` — env-var presence checks scoped to profile target
* ``mdk promote`` — cross-profile snapshot promotion with eval gates
"""

from __future__ import annotations

from movate.profiles.store import (
    Profile,
    ProfileNotFoundError,
    ProfileRegistry,
    ProfileStoreError,
    get_active_profile,
    load_registry,
    set_active_profile,
)

__all__ = [
    "Profile",
    "ProfileNotFoundError",
    "ProfileRegistry",
    "ProfileStoreError",
    "get_active_profile",
    "load_registry",
    "set_active_profile",
]
