"""CLI — ``mdk trigger create|list|delete`` (ADR 017 D2).

Covers:

* ``mdk trigger create`` registers a trigger under the local tenant, prints
  the webhook path + the secret ONCE (to stderr), and stores a hashed secret.
* ``--name`` overrides the handle; ``--kind workflow`` is accepted;
  ``--kind eval`` is rejected; ``--input-defaults`` parses JSON;
  ``--disabled`` creates a dormant trigger.
* ``mdk trigger list`` shows the trigger (no secret); ``--format json``
  round-trips.
* ``mdk trigger delete`` removes it (friendly no-op when missing).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.auth import verify_secret
from movate.core.models import JobKind
from movate.storage import build_storage

runner = CliRunner(mix_stderr=False)


@pytest.fixture
def local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "local.db"
    monkeypatch.setenv("MOVATE_DB", str(db_path))
    monkeypatch.setenv("MOVATE_TRACER", "silent")
    return db_path


async def _list_triggers() -> list:
    storage = build_storage()
    await storage.init()
    try:
        return await storage.list_triggers(tenant_id="local")
    finally:
        await storage.close()


@pytest.mark.unit
def test_create_then_list(local_db: Path) -> None:
    r = runner.invoke(
        app,
        ["trigger", "create", "triage-agent", "--input-defaults", '{"source": "zd"}'],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    # Webhook path on stdout; the secret + save-now warning on stderr.
    assert "/api/v1/triggers/" in r.stdout
    assert "never shown again" in r.stderr
    assert "secret:" in r.stderr

    rows = asyncio.run(_list_triggers())
    assert len(rows) == 1
    t = rows[0]
    assert t.name == "triage-agent"
    assert t.kind == JobKind.AGENT
    assert t.target == "triage-agent"
    assert t.input_defaults == {"source": "zd"}
    assert t.enabled is True
    # Stored secret is a hash, not plaintext.
    assert t.secret_hash and t.salt

    lst = runner.invoke(app, ["trigger", "list"])
    assert lst.exit_code == 0
    assert "triage-agent" in lst.stdout
    # The list view never prints the secret hash material.
    assert t.secret_hash not in lst.stdout


@pytest.mark.unit
def test_create_secret_verifies_against_stored_hash(local_db: Path) -> None:
    r = runner.invoke(app, ["trigger", "create", "triage-agent", "--format", "json"])
    assert r.exit_code == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    secret = payload["secret"]
    salt = payload["salt"]
    rows = asyncio.run(_list_triggers())
    assert len(rows) == 1
    # The one-time secret verifies against the persisted hash (hashed at rest).
    assert verify_secret(secret, rows[0].secret_hash, salt) is True


@pytest.mark.unit
def test_create_workflow_with_name_and_disabled(local_db: Path) -> None:
    r = runner.invoke(
        app,
        [
            "trigger",
            "create",
            "returns-pipeline",
            "--kind",
            "workflow",
            "--name",
            "nightly-returns",
            "--disabled",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    rows = asyncio.run(_list_triggers())
    assert len(rows) == 1
    assert rows[0].name == "nightly-returns"
    assert rows[0].kind == JobKind.WORKFLOW
    assert rows[0].enabled is False


@pytest.mark.unit
def test_create_rejects_eval_kind(local_db: Path) -> None:
    r = runner.invoke(app, ["trigger", "create", "faq", "--kind", "eval"])
    assert r.exit_code == 2
    assert "agent" in r.stderr and "workflow" in r.stderr


@pytest.mark.unit
def test_delete_removes_trigger(local_db: Path) -> None:
    runner.invoke(app, ["trigger", "create", "faq", "--name", "t1"])
    assert len(asyncio.run(_list_triggers())) == 1
    r = runner.invoke(app, ["trigger", "delete", "t1"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert asyncio.run(_list_triggers()) == []
    # Deleting a missing trigger is a friendly no-op (exit 0).
    again = runner.invoke(app, ["trigger", "delete", "t1"])
    assert again.exit_code == 0
