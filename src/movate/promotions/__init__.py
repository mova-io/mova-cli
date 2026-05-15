"""``mdk promote`` — gated cross-profile snapshot tagging (Sprint O Day 12-13).

A *promotion* records the decision "snapshot S is now the canonical
state for profile P." It's the audit-trail half of the dev → staging
→ prod flow. The actual config restoration happens via
``mdk migrate`` / ``mdk rollback``; promote is what makes that
decision *legible* later.

Storage: ``<project_root>/.movate/promotions.yaml``, append-only.
Each entry captures:

* the snapshot hash being promoted
* the destination profile name
* a timestamp + the operator note
* (optional) the eval score the operator observed before promoting

Reading the log answers two questions:

  - "What's currently in prod?"  → most recent entry with profile=prod
  - "When did staging change last?" → most recent entry with profile=staging

Eval gating is **opt-in** via ``--eval-pass-rate <float>`` — operators
who want hard enforcement run ``mdk eval`` separately and pass the
observed score. A future enhancement folds eval+promote into one
atomic ``--require-eval <agent>`` flag.

Design lock-ins:

* **Append-only.** Promotions are an immutable audit trail. The CLI
  has no ``unpromote`` command — fix mistakes by promoting a different
  snapshot, never by editing the log.
* **Project-scoped.** The log lives in the project's ``.movate/``
  directory, not ``~/.movate/``. Snapshots are project-local (their
  hashes only make sense relative to one project's files), so
  promotions inherit that scoping.
* **Profile names are not validated.** You can promote to any string
  the operator passes — same as ``mdk profiles`` accepts anything.
  Typo prevention is a CLI concern; the log itself is permissive.
"""

from __future__ import annotations

from movate.promotions.store import (
    Promotion,
    PromotionsLog,
    PromotionsStoreError,
    current_promotion,
    load_log,
    save_log,
)

__all__ = [
    "Promotion",
    "PromotionsLog",
    "PromotionsStoreError",
    "current_promotion",
    "load_log",
    "save_log",
]
