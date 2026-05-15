"""`TargetConfig` accepts operator-added custom fields without erroring.

Real-world failure mode (May 2026): operators annotate their
~/.movate/config.yaml target entries with org-specific URLs (Azure
portal deeplinks, internal devops dashboards, GitHub org URLs) for
convenience. Pre-fix the loader rejected those fields with
`extra="forbid"`, blocking `mdk deploy` until the operator manually
removed them from a file that's mostly hand-edited.

Fix: `TargetConfig` switches to `extra="allow"`. Pydantic v2's
`model_extra` mechanism preserves the unknown fields on rewrite, so
operators don't lose their annotations when the CLI re-saves the
config (e.g. on `mdk config use <name>` or `mdk config add-target`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from movate.core.user_config import TargetConfig, UserConfig, save_user_config


@pytest.mark.unit
class TestTargetConfigAllowsExtras:
    def test_extra_fields_load_without_error(self) -> None:
        """The headline regression — config with extra fields parses."""
        cfg = TargetConfig.model_validate(
            {
                "url": "https://example.com",
                "key_env": "MDK_DEV_KEY",
                # Custom operator annotations that previously errored:
                "azure_portal_url": "https://portal.azure.com/...",
                "azure_devops_url": "https://dev.azure.com/org/proj",
                "github_org_url": "https://github.com/example-org",
            }
        )
        assert cfg.url == "https://example.com"
        assert cfg.key_env == "MDK_DEV_KEY"

    def test_extras_preserved_in_model_extra(self) -> None:
        """Pydantic v2 stores unknown fields on ``model_extra``."""
        cfg = TargetConfig.model_validate(
            {
                "url": "https://example.com",
                "key_env": "K",
                "custom_link": "https://docs.example.com",
            }
        )
        # model_extra carries the unknown field for later access.
        assert cfg.model_extra is not None
        assert cfg.model_extra.get("custom_link") == "https://docs.example.com"

    def test_extras_round_trip_through_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the CLI re-saves the config, custom fields survive —
        operators don't lose their annotations after `mdk config use`
        or `mdk config add-target`."""
        config_path = tmp_path / "config.yaml"
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config_path))

        cfg = UserConfig(
            targets={
                "dev-personal": TargetConfig.model_validate(
                    {
                        "url": "https://dev.example.com",
                        "key_env": "MDK_DEV_KEY",
                        "azure_portal_url": "https://portal.azure.com/...",
                        "azure_devops_url": "https://dev.azure.com/...",
                        "github_org_url": "https://github.com/...",
                    }
                )
            },
            active="dev-personal",
        )
        save_user_config(cfg)

        # Round-trip through the file: the custom fields are still there.
        raw = yaml.safe_load(config_path.read_text())
        target = raw["targets"]["dev-personal"]
        assert target["url"] == "https://dev.example.com"
        assert target["azure_portal_url"].startswith("https://portal.azure.com")
        assert target["azure_devops_url"].startswith("https://dev.azure.com")
        assert target["github_org_url"].startswith("https://github.com")

    def test_known_fields_still_validate_types(self) -> None:
        """`extra="allow"` doesn't disable validation on KNOWN fields.
        A wrong-typed url still rejects."""
        with pytest.raises(Exception):
            TargetConfig.model_validate(
                {"url": 12345, "key_env": "K"}  # url must be string
            )

    def test_realworld_dev_personal_target_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact shape from the user's actual ~/.movate/config.yaml
        that triggered the regression. If this passes, `mdk deploy`
        no longer blocks on extra fields."""
        from movate.core.user_config import load_user_config  # noqa: PLC0415

        config_path = tmp_path / "config.yaml"
        monkeypatch.setenv("MOVATE_CONFIG_PATH", str(config_path))
        config_path.write_text(
            "targets:\n"
            "  dev-personal:\n"
            "    url: https://example.azurecontainerapps.io\n"
            "    key_env: MDK_DEV_KEY\n"
            "    azure_subscription: 8fab0f8f-b577-45d7-a485-ec32f73b22be\n"
            "    azure_resource_group: movate-dev-rg\n"
            "    azure_acr_name: movatedevacrmvt\n"
            "    azure_env: dev\n"
            "    azure_portal_url: https://portal.azure.com/...\n"
            "    azure_devops_url: https://dev.azure.com/...\n"
            "    github_org_url: https://github.com/mova-io\n"
            "active: dev-personal\n"
        )

        cfg = load_user_config()  # must not raise
        assert "dev-personal" in cfg.targets
        target = cfg.targets["dev-personal"]
        assert target.azure_resource_group == "movate-dev-rg"
        assert target.azure_acr_name == "movatedevacrmvt"
