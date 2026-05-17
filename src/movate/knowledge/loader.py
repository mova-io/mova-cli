"""Load a :class:`KnowledgeConfig` from ``knowledge.yaml`` and build
the configured retriever. Single function — :func:`build_retriever` —
because the v0.7 surface deliberately doesn't add a multi-source
abstraction. v0.8 will introduce that when we have real corpora and
real recall data to motivate the shape.

Two construction paths:

* :func:`load_knowledge_config` — parse a ``knowledge.yaml`` file on
  disk; raises a clean :class:`KnowledgeLoadError` for shape / schema
  problems so the CLI can surface the file path + line context.
* :func:`build_retriever` — config → :class:`Retriever`; the
  one-stop helper for ``mdk knowledge query`` and any future
  workflow retriever node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from movate.core.models import KnowledgeConfig, KnowledgeRetrieverKind
from movate.knowledge.retriever import BM25Retriever, Retriever, SubstringRetriever
from movate.knowledge.store import InMemoryCorpus, KnowledgeStoreError


class KnowledgeLoadError(ValueError):
    """Raised when ``knowledge.yaml`` is missing, unparseable, or
    doesn't validate against :class:`KnowledgeConfig`.

    Messages always include the file path so the operator doesn't
    have to grep their project tree to find the broken config.
    """


def load_knowledge_config(path: str | Path) -> KnowledgeConfig:
    """Parse a ``knowledge.yaml`` file into a validated
    :class:`KnowledgeConfig`.

    The file MUST be a YAML mapping. ``api_version`` and ``kind``
    fields are accepted but ignored — they're informational labels
    today; future versions may dispatch on them.
    """
    p = Path(path)
    if not p.is_file():
        raise KnowledgeLoadError(f"knowledge config not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise KnowledgeLoadError(f"invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise KnowledgeLoadError(f"knowledge config {p} is empty")
    if not isinstance(raw, dict):
        raise KnowledgeLoadError(
            f"knowledge config {p} must be a mapping, got {type(raw).__name__}"
        )
    # Strip api_version + kind — they're declarative labels, not
    # KnowledgeConfig fields, and `extra='forbid'` would reject them.
    payload: dict[str, Any] = {
        k: v for k, v in raw.items() if k not in ("api_version", "kind")
    }
    try:
        return KnowledgeConfig.model_validate(payload)
    except Exception as exc:
        raise KnowledgeLoadError(f"knowledge config {p} is invalid: {exc}") from exc


def build_retriever(
    cfg: KnowledgeConfig, *, base_dir: Path | None = None
) -> Retriever:
    """Construct the retriever the config asks for.

    ``cfg.corpus`` is resolved relative to ``base_dir`` (typically the
    agent directory containing ``knowledge.yaml``). Absolute paths are
    honored as-is.
    """
    corpus_path = Path(cfg.corpus)
    if not corpus_path.is_absolute() and base_dir is not None:
        corpus_path = (base_dir / corpus_path).resolve()
    try:
        corpus = InMemoryCorpus.from_path(corpus_path)
    except KnowledgeStoreError as exc:
        raise KnowledgeLoadError(str(exc)) from exc

    kwargs = {
        "body_fields": cfg.body_fields,
        "tag_field": cfg.tag_field,
        "id_field": cfg.id_field,
    }
    if cfg.retriever == KnowledgeRetrieverKind.BM25:
        return BM25Retriever(corpus, **kwargs)
    if cfg.retriever == KnowledgeRetrieverKind.SUBSTRING:
        return SubstringRetriever(corpus, **kwargs)
    raise KnowledgeLoadError(f"unknown retriever kind: {cfg.retriever}")
