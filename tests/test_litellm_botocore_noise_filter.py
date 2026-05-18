"""LiteLLM emits two import-time WARNING records when ``boto3`` /
``botocore`` aren't installed:

  * "litellm: could not pre-load bedrock-runtime response stream shape …"
  * "litellm: could not pre-load sagemaker-runtime response stream shape …"

These end up at the top of every ``mdk`` invocation as ~2 lines of
noise above the actual output. ``movate.providers.litellm`` installs
a logging filter that drops only those two patterns; other
LiteLLM WARNINGs (rate limits, model-not-found) still pass through.

Tests:

* The filter drops both specific botocore messages.
* The filter does NOT drop unrelated LiteLLM messages (regression
  guard: we don't want to suppress real warnings).
* The filter is installed on the ``LiteLLM`` logger at import time.
* End-to-end via subprocess: ``mdk --version`` stderr is silent on
  the botocore patterns.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from movate.providers.litellm import _LiteLLMBotocoreNoiseFilter


@pytest.mark.unit
def test_filter_drops_bedrock_runtime_warning() -> None:
    f = _LiteLLMBotocoreNoiseFilter()
    record = logging.LogRecord(
        name="LiteLLM",
        level=logging.WARNING,
        pathname="common_utils.py",
        lineno=979,
        msg=(
            "litellm: could not pre-load bedrock-runtime response stream shape "
            "— Bedrock event-stream decoding will be unavailable. "
            "Error: No module named 'botocore'"
        ),
        args=None,
        exc_info=None,
    )
    assert f.filter(record) is False


@pytest.mark.unit
def test_filter_drops_sagemaker_runtime_warning() -> None:
    f = _LiteLLMBotocoreNoiseFilter()
    record = logging.LogRecord(
        name="LiteLLM",
        level=logging.WARNING,
        pathname="common_utils.py",
        lineno=24,
        msg=(
            "litellm: could not pre-load sagemaker-runtime response stream shape "
            "— SageMaker event-stream decoding will be unavailable. "
            "Error: No module named 'botocore'"
        ),
        args=None,
        exc_info=None,
    )
    assert f.filter(record) is False


@pytest.mark.unit
def test_filter_passes_unrelated_litellm_warnings() -> None:
    """Regression guard: legitimate LiteLLM warnings (rate limits,
    model misconfigs, etc.) must still surface. Don't silence the
    whole logger — just the two known botocore messages."""
    f = _LiteLLMBotocoreNoiseFilter()
    for unrelated in [
        "litellm: rate limit reached for openai/gpt-4o, backing off",
        "litellm: model not found, falling back to anthropic/claude-haiku",
        "litellm: timeout after 30s, retrying",
        "litellm: tokenizer mismatch for model",
    ]:
        record = logging.LogRecord(
            name="LiteLLM",
            level=logging.WARNING,
            pathname="x.py",
            lineno=1,
            msg=unrelated,
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is True, (
            f"unrelated LiteLLM message was wrongly silenced: {unrelated!r}"
        )


@pytest.mark.unit
def test_filter_is_installed_on_litellm_logger_after_import() -> None:
    """Importing ``movate.providers.litellm`` installs the filter on
    Python's ``LiteLLM`` logger. Pin this so a future refactor can't
    accidentally drop the install hook."""
    handlers_and_filters = logging.getLogger("LiteLLM").filters
    assert any(isinstance(f, _LiteLLMBotocoreNoiseFilter) for f in handlers_and_filters), (
        "_LiteLLMBotocoreNoiseFilter not attached to the LiteLLM logger; "
        "the install hook in movate.providers.litellm must run at import time"
    )


@pytest.mark.unit
def test_filter_is_installed_at_package_import_not_provider_import() -> None:
    """Defense in depth: the filter must be installed on ``LiteLLM``
    by the time ``import movate`` returns — BEFORE any provider
    module is touched. Otherwise a future code path that triggers
    ``import litellm`` before importing ``movate.providers.litellm``
    would leak the botocore warnings.

    Verified via subprocess so the LiteLLM logger isn't polluted by
    a filter another test already installed."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import movate, logging; "
                "from movate import _LiteLLMBotocoreNoiseFilter; "
                "filters = logging.getLogger('LiteLLM').filters; "
                "assert any(isinstance(f, _LiteLLMBotocoreNoiseFilter) for f in filters), "
                "'filter not installed by movate package import'; "
                "print('ok')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


@pytest.mark.unit
def test_filter_install_is_idempotent_across_reimports() -> None:
    """Re-importing ``movate`` (e.g. during a test reload) must not
    stack duplicate filter instances on the ``LiteLLM`` logger. A
    growing filter list would compound startup cost over a long
    test session."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import movate, importlib, logging; "
                "from movate import _LiteLLMBotocoreNoiseFilter; "
                "importlib.reload(movate); "
                "importlib.reload(movate); "
                "count = sum(1 for f in logging.getLogger('LiteLLM').filters "
                "if isinstance(f, _LiteLLMBotocoreNoiseFilter)); "
                "assert count == 1, f'expected 1 filter, got {count}'; "
                "print('ok')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


@pytest.mark.unit
def test_mdk_version_subprocess_emits_no_botocore_warnings() -> None:
    """End-to-end via subprocess: ``mdk --version`` is one of the
    cheapest CLI commands. Its stderr/stdout must not contain either
    botocore-related warning. Subprocess-based so we get a fresh
    Python interpreter (no leftover logging filter from the current
    test process)."""
    result = subprocess.run(
        [sys.executable, "-m", "movate.cli.main", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "bedrock-runtime response stream shape" not in combined
    assert "sagemaker-runtime response stream shape" not in combined
