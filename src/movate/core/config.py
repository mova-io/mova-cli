"""Project-wide movate configuration loaded from ``movate.yaml``.

Lets a teammate run ``movate bench faq-agent --input ...`` without remembering
every model id. Defaults from ``movate.yaml`` at the project root; CLI flags
always override.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    judges: list[str] = Field(default_factory=list)
    runs: int = 1


class EvalDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: float | None = None


class ProjectConfig(BaseModel):
    """Project-wide defaults — overrideable via CLI flags."""

    model_config = ConfigDict(extra="forbid")

    agents_dir: str = "./agents"
    workflows_dir: str = "./workflows"
    bench: BenchConfig = Field(default_factory=BenchConfig)
    eval: EvalDefaults = Field(default_factory=EvalDefaults)


def load_project_config(path: Path | str | None = None) -> ProjectConfig:
    """Load ``movate.yaml`` from the project root (or provided path).

    Returns defaults if the file is absent. Errors out clearly on a malformed
    file — never silently degrades on a typo.
    """
    p = Path(path) if path else Path("movate.yaml")
    if not p.exists():
        return ProjectConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return ProjectConfig.model_validate(data)
