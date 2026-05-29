"""Multi-runtime target resolution for the playground (pure logic).

The playground can run in two modes:

* **single-runtime** (the original, unchanged behavior) — one runtime URL
  + one bearer token, taken from ``--runtime-url`` / ``--api-key`` (or the
  ``MDK_PLAYGROUND_*`` / ``MOVATE_*`` env vars). No target picker.
* **multi-target** — the operator has registered several deployment
  targets in ``~/.movate/config.yaml`` (the same file ``mdk config
  list-targets`` reads). The playground surfaces ONE Chainlit chat
  profile per configured target; selecting a profile points that
  session at THAT target's runtime URL + bearer token, then the existing
  agent picker lists that target's agents.

This module is **pure** (no Chainlit import, no network): it maps the
user config + environment into a list of :class:`PlaygroundTarget`
records and handles the env-encoding contract between the CLI launcher
(:mod:`movate.cli.playground`) and the Chainlit app
(:mod:`movate.playground.app`). Chainlit's process model has no typed
way to hand structured args to the app module, so the launcher serializes
the resolved targets into a single env var (:data:`TARGETS_ENV_VAR`) that
the app deserializes at import. Keeping that contract here — alongside a
dataclass and JSON round-trip — makes it unit-testable on a no-extras
install.

Bearer tokens are resolved from the per-target ``key_env`` env var
(e.g. ``MDK_DEV_KEY``), which ``movate.credentials.loader`` already
autoloads from ``~/.movate/credentials`` at CLI startup. A target whose
key env var is unset is **not** an error — it's surfaced as a disabled
profile with a hint, so a missing key degrades gracefully instead of
crashing the whole UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

#: Env var the launcher uses to hand the resolved target list to the app
#: module. Holds a JSON array of :meth:`PlaygroundTarget.to_dict` records.
#: Its presence (non-empty) is what flips the app into multi-target mode;
#: absence keeps the original single-runtime path.
TARGETS_ENV_VAR = "MDK_PLAYGROUND_TARGETS"


@dataclass(frozen=True)
class PlaygroundTarget:
    """One runtime the playground can talk to in multi-target mode.

    Built from a ``~/.movate/config.yaml`` target. ``api_key`` is the
    RESOLVED bearer token (read from ``key_env`` at resolution time), or
    ``None`` when that env var was unset/empty — in which case
    :attr:`key_available` is False and the app shows the profile as
    disabled with a hint rather than letting a 401 surface as a stack
    trace.
    """

    name: str
    """Target name from the config (e.g. ``dev``, ``staging``, ``prod``)."""

    url: str
    """Runtime base URL (no trailing slash; the client strips one anyway)."""

    key_env: str
    """Name of the env var the bearer token comes from (e.g. ``MDK_DEV_KEY``)."""

    api_key: str | None = None
    """Resolved bearer token, or ``None`` when ``key_env`` was unset/empty."""

    @property
    def key_available(self) -> bool:
        """Whether a non-empty bearer token resolved for this target."""
        return bool(self.api_key)

    def profile_label(self) -> str:
        """Human label for the chat-profile picker — name + URL."""
        return f"{self.name} ({self.url})"

    def profile_description(self) -> str:
        """Markdown blurb under the profile name in the picker.

        When the key is missing we say so up front so the operator knows
        the profile won't work before selecting it (belt-and-suspenders
        with the in-chat hint :func:`movate.playground.app` shows).
        """
        if self.key_available:
            return f"Runtime: `{self.url}` — auth via `{self.key_env}`."
        return (
            f"⚠ Runtime: `{self.url}` — no key. Set `{self.key_env}` "
            "(e.g. `mdk auth login` / export it) to use this target."
        )

    def to_dict(self) -> dict[str, str | None]:
        """Serialize for the launcher→app env hand-off (JSON-safe)."""
        return {
            "name": self.name,
            "url": self.url,
            "key_env": self.key_env,
            "api_key": self.api_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PlaygroundTarget:
        """Inverse of :meth:`to_dict` — tolerant of missing optional keys."""
        api_key = data.get("api_key")
        return cls(
            name=str(data.get("name", "")),
            url=str(data.get("url", "")),
            key_env=str(data.get("key_env", "")),
            api_key=str(api_key) if api_key else None,
        )


def resolve_targets_from_config(
    targets: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> list[PlaygroundTarget]:
    """Map configured targets → :class:`PlaygroundTarget` list.

    ``targets`` is the ``UserConfig.targets`` mapping (name → object with
    ``.url`` / ``.key_env`` attributes — a ``TargetConfig``). We read it
    structurally (duck-typed) so this module never imports the config
    model, keeping it dependency-light and the contract narrow.

    Each target's bearer token is resolved from its ``key_env`` env var
    (already autoloaded from ``~/.movate/credentials``). A target whose
    key is unset still appears in the result with ``api_key=None`` /
    ``key_available=False`` — the launcher keeps it so the picker can
    show it disabled with a hint, never silently dropping a target the
    operator registered.

    Targets are returned sorted by name for a stable picker order.
    """
    environ = env if env is not None else os.environ
    resolved: list[PlaygroundTarget] = []
    for name in sorted(targets):
        t = targets[name]
        url = str(getattr(t, "url", "") or "")
        key_env = str(getattr(t, "key_env", "") or "")
        if not url:
            # A target with no URL is malformed config we can't talk to;
            # skip rather than building an unusable profile.
            continue
        token = environ.get(key_env, "").strip() if key_env else ""
        resolved.append(
            PlaygroundTarget(
                name=name,
                url=url,
                key_env=key_env,
                api_key=token or None,
            )
        )
    return resolved


def encode_targets(targets: list[PlaygroundTarget]) -> str:
    """Serialize targets to the JSON string carried in :data:`TARGETS_ENV_VAR`.

    The resolved bearer tokens ride in the SERVER process env only (the
    launcher sets this on the child ``chainlit run`` process env) — never
    in browser JS, never logged. Returns ``""`` for an empty list so the
    launcher can simply skip setting the env var (absence = single-runtime).
    """
    if not targets:
        return ""
    return json.dumps([t.to_dict() for t in targets])


def decode_targets(raw: str | None) -> list[PlaygroundTarget]:
    """Parse the :data:`TARGETS_ENV_VAR` JSON back into targets.

    Tolerant: an unset / empty / malformed value yields an empty list,
    so the app falls back to single-runtime mode instead of crashing on
    a corrupt hand-off (defense in depth — the launcher only ever writes
    well-formed JSON).
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[PlaygroundTarget] = []
    for item in data:
        if isinstance(item, dict):
            out.append(PlaygroundTarget.from_dict(item))
    return out


__all__ = [
    "TARGETS_ENV_VAR",
    "PlaygroundTarget",
    "decode_targets",
    "encode_targets",
    "resolve_targets_from_config",
]
