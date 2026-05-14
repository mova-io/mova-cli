"""Load ``knowledge.yaml`` + ingest documents into a :class:`KnowledgeStore`.

The MVP schema:

  api_version: movate/v1
  kind: Knowledge
  documents:
    - id: contracts-glossary
      path: ./docs/contracts-glossary.md
      description: Glossary of contract terms
      tags: [contracts, glossary]

Paths are resolved relative to the knowledge.yaml file. Today we only
support text + markdown bodies; PDF / Word / HTML ingestion lands in
v0.8 (depends on a parser dep we don't want in the MVP).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from movate.knowledge.store import (
    Document,
    InMemoryStore,
    KnowledgeStore,
    make_document,
)


class KnowledgeLoadError(Exception):
    """Raised on malformed ``knowledge.yaml`` or unresolvable doc paths.

    Always carries an operator-facing message — the CLI surfaces this
    directly with an exit-2 status. Loading should fail loud, not
    silently produce an empty store.
    """


_SUPPORTED_EXTENSIONS = frozenset({".md", ".txt", ".markdown"})


@dataclass(frozen=True)
class KnowledgeConfig:
    """Parsed knowledge.yaml — the registration metadata before ingestion.

    Kept as a separate type from :class:`Document` because the config
    references files on disk; the Document is the loaded in-memory
    artifact. Splitting the two lets ``mdk knowledge list`` show
    declared documents without forcing a full load.
    """

    api_version: str
    kind: str
    documents: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    source_path: Path | None = None


def load_knowledge(
    knowledge_yaml_path: str | Path,
    *,
    store: KnowledgeStore | None = None,
) -> KnowledgeStore:
    """Parse ``knowledge.yaml`` and ingest every referenced document.

    Returns the populated :class:`KnowledgeStore`. If ``store`` is
    ``None``, a fresh :class:`InMemoryStore` is created — callers
    that want a pre-existing store (e.g. tests with seeded docs)
    pass it in.

    Raises :class:`KnowledgeLoadError` on:

    * Missing knowledge.yaml
    * Malformed YAML
    * Unsupported api_version / kind
    * Document path that doesn't resolve
    * Unsupported document file extension

    Documents are ingested in declaration order; later docs with
    duplicate ids overwrite earlier ones (operator decision).
    """
    config = _parse_config(Path(knowledge_yaml_path))
    target_store = store if store is not None else InMemoryStore()
    for entry in config.documents:
        doc = _ingest_document(entry, knowledge_root=config.source_path)
        target_store.add(doc)
    return target_store


def _parse_config(path: Path) -> KnowledgeConfig:
    """Read + validate the top-level shape of knowledge.yaml."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise KnowledgeLoadError(f"knowledge.yaml not found at {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as exc:
        raise KnowledgeLoadError(f"knowledge.yaml is not valid YAML: {exc}") from exc

    if raw is None:
        # Empty file — treat as an empty knowledge base. Permissive
        # so `mdk knowledge add` can populate from scratch.
        raw = {}
    if not isinstance(raw, dict):
        raise KnowledgeLoadError(f"knowledge.yaml root must be a mapping; got {type(raw).__name__}")

    api_version = str(raw.get("api_version") or "movate/v1")
    kind = str(raw.get("kind") or "Knowledge")
    if api_version != "movate/v1":
        raise KnowledgeLoadError(f"unsupported api_version {api_version!r}; expected 'movate/v1'")
    if kind != "Knowledge":
        raise KnowledgeLoadError(f"unsupported kind {kind!r}; expected 'Knowledge'")

    raw_documents = raw.get("documents") or []
    if not isinstance(raw_documents, list):
        raise KnowledgeLoadError("'documents' must be a list")

    return KnowledgeConfig(
        api_version=api_version,
        kind=kind,
        documents=tuple(raw_documents),
        source_path=resolved.parent,
    )


def _ingest_document(entry: dict, *, knowledge_root: Path | None) -> Document:
    """Resolve a single document entry → loaded body → :class:`Document`.

    Validates required fields (id + path), resolves the path relative
    to knowledge.yaml's directory, checks the file extension, reads
    the body, and constructs a content-hashed :class:`Document`.
    """
    if not isinstance(entry, dict):
        raise KnowledgeLoadError(f"document entry must be an object; got {type(entry).__name__}")

    doc_id = str(entry.get("id") or "").strip()
    raw_path = str(entry.get("path") or "").strip()
    if not doc_id:
        raise KnowledgeLoadError(f"document missing 'id': {entry}")
    if not raw_path:
        raise KnowledgeLoadError(f"document {doc_id!r} missing 'path'")

    base = knowledge_root or Path.cwd()
    doc_path = (base / raw_path).resolve()
    if not doc_path.is_file():
        raise KnowledgeLoadError(f"document {doc_id!r} path {doc_path} does not exist")
    if doc_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise KnowledgeLoadError(
            f"document {doc_id!r} has unsupported extension {doc_path.suffix!r}; "
            f"supported: {sorted(_SUPPORTED_EXTENSIONS)}"
        )

    body = doc_path.read_text(encoding="utf-8")
    description = str(entry.get("description") or "")
    tags_raw = entry.get("tags") or []
    if not isinstance(tags_raw, list):
        raise KnowledgeLoadError(
            f"document {doc_id!r} 'tags' must be a list; got {type(tags_raw).__name__}"
        )
    tags = tuple(str(t) for t in tags_raw)

    return make_document(
        doc_id=doc_id,
        body=body,
        description=description,
        tags=tags,
        source_path=str(doc_path),
    )
