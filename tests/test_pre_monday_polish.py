"""Tests for the pre-Monday polish batch.

Five items, all surfaced by Saturday morning's end-to-end smoke test
of the demo flow:

1. ``mdk init`` scaffolds canonical READMEs in ``contexts/`` and
   ``skills/`` (same shape as the existing ``kb/README.md``).
2. ``mdk deploy`` preflight: error early when cwd has no
   ``Dockerfile`` (instead of wasting 2-4 minutes on a doomed
   ``az acr build`` upload).
3. ``mdk deploy`` dry-run header says ``mdk deploy`` (not the legacy
   ``movate deploy``).
4. ``mdk add`` multi-agent workspace Panel uses canonical commands
   (``mdk validate --all``, ``mdk eval --all --mock --gate 0.7``,
   ``mdk deploy --target <active>``) instead of stale ones
   (``mdk ci eval --mock``, ``--target prod``).
5. ``mdk init`` Panel lists the four supported auth providers
   instead of a bare ``<provider>`` placeholder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# #1 — canonical READMEs in contexts/ + skills/
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_scaffolds_contexts_readme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk init <name>` should drop a canonical README.md in
    `contexts/` explaining what files go there. Same shape as the
    existing kb/README.md so all three convention dirs document
    themselves in-place."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    readme = tmp_path / "demo" / "contexts" / "README.md"
    assert readme.is_file(), "contexts/README.md should be scaffolded"
    content = readme.read_text()
    # Section structure mirrors kb/README.md.
    assert "What goes here" in content
    assert "Conventions" in content
    # Specific guidance about prepending to prompts.
    assert "prepended to agent prompts" in content
    # Mention of the per-agent override pattern.
    assert "agents/" in content and "contexts/" in content


@pytest.mark.unit
def test_init_scaffolds_skills_readme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk init <name>` should drop a canonical README.md in
    `skills/` explaining the python/http/mcp backend pattern."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    readme = tmp_path / "demo" / "skills" / "README.md"
    assert readme.is_file(), "skills/README.md should be scaffolded"
    content = readme.read_text()
    assert "What goes here" in content
    assert "Conventions" in content
    # Specific guidance about the three backend types.
    assert "python" in content.lower()
    assert "http" in content.lower()
    assert "mcp" in content.lower()
    # skill.yaml + impl.py mentioned by name.
    assert "skill.yaml" in content
    assert "impl.py" in content


@pytest.mark.unit
def test_init_kb_readme_still_scaffolded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the existing kb/README.md must still ship after
    we added the two new ones."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert (tmp_path / "demo" / "kb" / "README.md").is_file()


# ---------------------------------------------------------------------------
# #2 — `mdk deploy` preflight: error if no Dockerfile in cwd
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_preflight_errors_when_no_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running `mdk deploy` from a customer project dir (which has
    no Dockerfile — only the movate-cli source tree does) should
    error immediately, NOT spend 2-4 min on a doomed ACR build."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    # Register a fake target so we don't error on missing target first.
    result = runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    # tmp_path has no Dockerfile — deploy should error before any
    # `az` invocation.
    assert not (tmp_path / "Dockerfile").exists()
    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    # The error explains the missing Dockerfile.
    assert "no Dockerfile" in combined.lower() or "dockerfile" in combined.lower()
    # The hint mentions the two paths forward.
    assert "movate-cli" in combined.lower() or "runtime image" in combined.lower()


@pytest.mark.unit
def test_deploy_preflight_skipped_under_skip_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--skip-build` means we won't run `az acr build` — so the
    no-Dockerfile preflight should NOT fire. Operator is rolling
    Container Apps to a pre-built image."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    # --skip-build + --image-tag + --dry-run should pass preflight
    # AND not actually invoke any Azure mutation.
    result = runner.invoke(
        app,
        [
            "deploy",
            "--target",
            "fake",
            "--skip-build",
            "--image-tag",
            "movate:0.7.0-test",
            "--dry-run",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Did NOT hit the no-Dockerfile error.
    combined = result.stdout + result.stderr
    assert "no Dockerfile" not in combined


# ---------------------------------------------------------------------------
# #3 — dry-run header brands as `mdk` not `movate`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_dry_run_header_uses_mdk_branding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run plan header should say `mdk deploy → <target>`,
    not the legacy `movate deploy → <target>`. The binary alias
    still works; mixing names in user-facing strings is confusing."""
    # Need a Dockerfile present so the preflight passes — write one.
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    result = runner.invoke(app, ["deploy", "--target", "fake", "--dry-run"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    # Canonical brand appears.
    assert "mdk deploy" in combined
    # Legacy brand does NOT appear in this header. (It may legitimately
    # appear elsewhere — e.g. in install hints — so we check just the
    # plan header line shape.)
    plan_header_line = next(
        (line for line in combined.splitlines() if "deploy" in line and "→" in line),
        "",
    )
    assert "movate deploy" not in plan_header_line


# ---------------------------------------------------------------------------
# #4 — workspace Panel uses canonical commands
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_workspace_panel_uses_domain_scoped_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mdk add <a> <b>`` end-of-batch picker should be DOMAIN-
    SCOPED to ``add``'s own concerns (2026-05-19 operator feedback):
    surface the IMMEDIATE-next actions on what we just scaffolded —
    validate the bundle, doctor-check the agent. Eval + deploy
    suggestions were removed because they're downstream concerns
    owned by their own commands' menus.

    Pre-2026-05-19 the menu showed all four (validate, eval, doctor,
    deploy) — the cross-domain noise drowned out the actually-useful
    "did my scaffold work" check.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq", "ticket-triager"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # In-domain commands present.
    assert "mdk validate --all" in result.stdout
    assert "mdk doctor agent" in result.stdout
    # Cross-domain commands MUST NOT surface (per the scoping principle).
    assert "mdk eval --all" not in result.stdout, (
        "post-2026-05-19 mdk add menu must NOT suggest eval — that's a downstream domain"
    )
    assert "mdk deploy" not in result.stdout, (
        "post-2026-05-19 mdk add menu must NOT suggest deploy — that's a downstream domain"
    )
    # Stale-command guards from the original test stay.
    assert "mdk ci eval" not in result.stdout
    assert "--target prod" not in result.stdout


# ---------------------------------------------------------------------------
# #5 — init API-keys footer lists providers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_api_keys_footer_lists_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "API keys" footer in `mdk init` should name the supported
    providers explicitly (`openai`, `anthropic`, `azure`, `gemini`)
    instead of a bare `<provider>` placeholder."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for provider in ("openai", "anthropic", "azure", "gemini"):
        assert provider in result.stdout, f"provider {provider!r} should be listed"
