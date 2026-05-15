"""LLM-driven scaffolding for movate artifacts.

Public surface:

* :class:`GeneratedAgent` — Pydantic payload an LLM scaffolder returns.
* :func:`generate_agent_from_description` — single generation attempt
  (caller owns the validation/retry loop).
* :func:`write_agent_files` — materialize a ``GeneratedAgent`` to disk
  in the standard movate file layout (``agent.yaml`` + ``prompt.md``
  + ``schema/{input,output}.json`` + ``evals/dataset.jsonl``).
* :exc:`LLMScaffoldError` — raised on malformed LLM output or wire
  errors. Phase 2 callers (``mdk init --llm``) catch this and trigger
  the retry loop.

The validation loop (write to tempdir → ``load_agent()`` → retry once
on failure → debug artifact on second failure) lives in the caller
(``movate.cli.init``) so this module stays pure generation + IO.
"""

from __future__ import annotations

from movate.scaffold.llm_scaffold import (
    GeneratedAgent,
    LLMScaffoldError,
    generate_agent_from_description,
    write_agent_files,
)

__all__ = [
    "GeneratedAgent",
    "LLMScaffoldError",
    "generate_agent_from_description",
    "write_agent_files",
]
