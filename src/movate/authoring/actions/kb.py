"""Ingest-KB catalog action — embed files into the agent's knowledge base (ADR 025 D1).

Composes the shipped ingest primitive :func:`movate.kb.ingest.ingest_path`
(read → chunk → embed → persist). This is the catalog's one **networked +
cost** action: embedding calls hit a provider API and spend money, so it
**always** requires confirmation (D2) and is **not** reversible via a file
checkpoint (chunks land in the StorageProvider, not in tracked files) — D2/D4
both classify it as confirm-gated.

The plan estimates the embedding cost from the file set without writing or
calling the network. The library takes ``storage`` + ``api_key`` by injection
(:class:`~movate.authoring.base.AuthoringContext`) — the catalog never reaches
for a ``~/.movate`` store or an env key on its own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from movate.authoring.base import AuthoringActionError, AuthoringContext, BaseAuthoringAction
from movate.authoring.models import ActionPlan, ActionResult, SideEffect
from movate.cli.kb_cmd import _estimate_embedding_cost
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL
from movate.kb.ingest import IngestSummary, find_files, ingest_path


class IngestKbArgs(BaseModel):
    """Args for :class:`IngestKbAction`."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(..., description="Agent whose KB the documents are ingested into.")
    path: str = Field(..., description="File or directory of documents to ingest.")
    tenant_id: str = Field(default="local", description="Tenant the chunks are stored under.")
    embedding_model: str = Field(default=DEFAULT_EMBEDDING_MODEL, description="Embedding model id.")
    clean_source: bool = Field(
        default=False, description="Delete existing chunks for each source before re-ingesting."
    )


class IngestKbAction(BaseAuthoringAction):
    """Ingest documents into an agent's knowledge base (the ``mdk kb ingest`` primitive).

    Networked + cost-incurring → always requires confirmation. Not reversible
    via a file checkpoint (chunks live in storage); the driver records the
    apply but does not auto-revert this action.
    """

    name = "ingest-kb"
    description = (
        "Ingest documents (a file or directory) into the agent's knowledge base "
        "— read, chunk, embed, and persist via the kb ingest primitive. Makes "
        "network calls and incurs embedding cost; ALWAYS requires confirmation. "
        "Not file-reversible (chunks live in storage)."
    )
    side_effects: tuple[SideEffect, ...] = (SideEffect.NETWORK, SideEffect.COST)
    reversible = False
    args_model: type[BaseModel] = IngestKbArgs

    def plan(self, ctx: AuthoringContext, args: IngestKbArgs) -> ActionPlan:
        src = Path(args.path).expanduser()
        if not src.exists():
            raise AuthoringActionError(f"ingest path not found: {src}")
        files = find_files(src)
        est = _estimate_embedding_cost(files)
        return ActionPlan(
            action=self.name,
            summary=(
                f"ingest {len(files)} file(s) from {src} into {args.agent!r}'s KB "
                f"(~${est:.4f} embedding cost)"
            ),
            diff="",
            side_effects=list(self.side_effects),
            reversible=False,
            requires_confirmation=True,  # networked + cost — always
            estimated_cost_usd=est,
            details={"file_count": len(files), "path": str(src), "tenant_id": args.tenant_id},
        )

    def apply(self, ctx: AuthoringContext, args: IngestKbArgs) -> ActionResult:
        storage = ctx.storage
        if storage is None:
            raise AuthoringActionError(
                "ingest-kb requires a storage provider; inject one via "
                "AuthoringContext.storage (the CLI/PR3 wires it after confirmation)"
            )
        src = Path(args.path).expanduser()
        if not src.exists():
            raise AuthoringActionError(f"ingest path not found: {src}")

        async def _run() -> tuple[list[IngestSummary], list[tuple[str, str]]]:
            return await ingest_path(
                storage=storage,
                path=src,
                agent=args.agent,
                tenant_id=args.tenant_id,
                embedding_model=args.embedding_model,
                api_key=ctx.api_key,
                clean_source=args.clean_source,
            )

        summaries, failed = asyncio.run(_run())
        chunks = sum(s.chunks_saved for s in summaries)
        cost = _estimate_embedding_cost(find_files(src))
        return ActionResult(
            action=self.name,
            summary=(
                f"ingested {len(summaries)} source(s) → {chunks} chunk(s) "
                f"into {args.agent!r}'s KB" + (f" ({len(failed)} failed)" if failed else "")
            ),
            changed_paths=[],  # chunks land in storage, not tracked files
            cost_usd=cost,
            details={
                "sources": len(summaries),
                "chunks": chunks,
                "failed": [f[0] for f in failed],
            },
        )
