"""Deploy-notification module — tests cover:

1. Telegram backend fires when both env vars are set.
2. Telegram backend skips when either env var is missing.
3. Telegram non-200 → returns False, logs warning, doesn't raise.
4. Generic webhook fires when MOVATE_DEPLOY_WEBHOOK is set.
5. Generic webhook handles 2xx variants (200, 201, 204) uniformly.
6. Generic webhook non-2xx → returns False, logs warning.
7. Both backends can fire on the same event.
8. Neither configured → notify_deploy_success is a no-op.
9. HTTP exception → returns False, doesn't raise.
10. Greppable mdk_notify_summary line is emitted on stderr.
11. Telegram message format includes target + version + image tag.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import pytest

from movate.notify.deploy_notify import (
    DeployEvent,
    _format_telegram_message,
    _try_telegram,
    _try_webhook,
    notify_deploy_success,
)


def _event(**overrides: object) -> DeployEvent:
    base = DeployEvent(
        target="prod",
        image_tag="movate:0.6.1-abc1234",
        runtime_url="https://prod-runtime.example.com",
        git_sha="abc1234",
        deployer="alice",
        duration_seconds=42.5,
        version="0.6.1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Item 1-3: Telegram backend
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelegram:
    def test_fires_when_both_env_vars_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = 200
        post_mock = MagicMock(return_value=fake_resp)
        monkeypatch.setattr(httpx, "post", post_mock)

        assert _try_telegram(_event()) is True
        # The URL contains the token; the body contains chat_id + text.
        call = post_mock.call_args
        assert "test-token" in call.args[0]
        assert call.kwargs["json"]["chat_id"] == "12345"
        assert "prod" in call.kwargs["json"]["text"]

    def test_skips_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        # If it didn't skip it would try to make an HTTP call. Verify
        # by ensuring httpx.post is never called.
        post_mock = MagicMock()
        monkeypatch.setattr(httpx, "post", post_mock)

        assert _try_telegram(_event()) is False
        post_mock.assert_not_called()

    def test_skips_when_chat_id_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        post_mock = MagicMock()
        monkeypatch.setattr(httpx, "post", post_mock)

        assert _try_telegram(_event()) is False
        post_mock.assert_not_called()

    def test_non_200_returns_false_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = 403
        fake_resp.text = "Forbidden"
        monkeypatch.setattr(httpx, "post", MagicMock(return_value=fake_resp))

        # Must not raise — notifications are observability, not control flow.
        assert _try_telegram(_event()) is False

    def test_http_exception_returns_false_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

        def boom(*args: object, **kwargs: object) -> object:
            raise httpx.HTTPError("connection refused")

        monkeypatch.setattr(httpx, "post", boom)
        assert _try_telegram(_event()) is False


# ---------------------------------------------------------------------------
# Item 4-6: Generic webhook
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWebhook:
    def test_fires_when_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com/deploy"
        )
        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = 200
        post_mock = MagicMock(return_value=fake_resp)
        monkeypatch.setattr(httpx, "post", post_mock)

        assert _try_webhook(_event()) is True
        # POST body is the full DeployEvent as JSON.
        call = post_mock.call_args
        assert call.args[0] == "https://hooks.example.com/deploy"
        body = call.kwargs["json"]
        assert body["target"] == "prod"
        assert body["image_tag"] == "movate:0.6.1-abc1234"
        assert body["version"] == "0.6.1"

    def test_skips_when_url_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MOVATE_DEPLOY_WEBHOOK", raising=False)
        post_mock = MagicMock()
        monkeypatch.setattr(httpx, "post", post_mock)

        assert _try_webhook(_event()) is False
        post_mock.assert_not_called()

    @pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
    def test_all_2xx_codes_count_as_success(
        self, status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slack returns 200, Discord returns 204 — both must work."""
        monkeypatch.setenv("MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com")
        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = status
        monkeypatch.setattr(httpx, "post", MagicMock(return_value=fake_resp))
        assert _try_webhook(_event()) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
    def test_non_2xx_returns_false(
        self, status: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com")
        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = status
        fake_resp.text = "error"
        monkeypatch.setattr(httpx, "post", MagicMock(return_value=fake_resp))
        assert _try_webhook(_event()) is False


# ---------------------------------------------------------------------------
# Item 7-8: notify_deploy_success orchestration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNotifyOrchestration:
    def test_both_backends_fire_independently(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        monkeypatch.setenv("MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com")

        fake_resp = MagicMock(spec=httpx.Response)
        fake_resp.status_code = 200
        post_mock = MagicMock(return_value=fake_resp)
        monkeypatch.setattr(httpx, "post", post_mock)

        notify_deploy_success(_event())
        # Two HTTP calls — one per backend.
        assert post_mock.call_count == 2

        # Summary line includes BOTH outcomes.
        captured = capsys.readouterr()
        assert "mdk_notify_summary:" in captured.err
        assert "telegram=true" in captured.err
        assert "webhook=true" in captured.err

    def test_neither_configured_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MOVATE_DEPLOY_WEBHOOK"):
            monkeypatch.delenv(key, raising=False)
        post_mock = MagicMock()
        monkeypatch.setattr(httpx, "post", post_mock)

        notify_deploy_success(_event())
        post_mock.assert_not_called()
        # Summary line still emitted — but both flags false.
        captured = capsys.readouterr()
        assert "telegram=false" in captured.err
        assert "webhook=false" in captured.err

    def test_one_backend_failing_doesnt_prevent_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        monkeypatch.setenv("MOVATE_DEPLOY_WEBHOOK", "https://hooks.example.com")

        def post_side_effect(url: str, **kwargs: object) -> MagicMock:
            resp = MagicMock(spec=httpx.Response)
            # Telegram URL fails; webhook succeeds.
            if "api.telegram.org" in url:
                resp.status_code = 500
                resp.text = "Internal Error"
            else:
                resp.status_code = 200
            return resp

        monkeypatch.setattr(httpx, "post", MagicMock(side_effect=post_side_effect))
        # Should not raise.
        notify_deploy_success(_event())


# ---------------------------------------------------------------------------
# Item 11: Telegram message format
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelegramFormat:
    def test_message_includes_required_fields(self) -> None:
        msg = _format_telegram_message(_event())
        assert "prod" in msg
        assert "movate:0.6.1-abc1234" in msg
        assert "abc1234" in msg  # git SHA
        assert "alice" in msg  # deployer
        assert "0.6.1" in msg  # version
        assert "https://prod-runtime.example.com" in msg

    def test_message_omits_git_sha_when_empty(self) -> None:
        msg = _format_telegram_message(_event(git_sha=""))
        # No "Git SHA:" line when sha is empty.
        assert "Git SHA" not in msg
