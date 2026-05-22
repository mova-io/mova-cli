<!--
Keep PRs single-responsibility. See CLAUDE.md + docs/architecture-principles.md.
Delete sections that genuinely don't apply, but don't skip them silently.
-->

## Summary

<!-- What changed and WHY (the why matters more than the what). 1–3 bullets. -->

-

## Scope check

- [ ] **Single responsibility** — this PR does one thing. (No bundled,
      unrelated changes.)
- [ ] **No opportunistic refactors** — only files necessary for this change
      were touched. Adjacent debt I noticed is documented (link), not silently
      fixed here.

## Design (for non-trivial changes)

<!-- Why this abstraction / dependency / interface? Alternatives considered?
     Blast radius — what could this break? Delete if a small fix. -->

- **Why this approach:**
- **Alternatives considered:**
- **Blast radius:**
- **ADR:** <!-- link docs/adr/NNN-*.md if this is a structural change, else "n/a — not structural" -->

## Backward compatibility

- [ ] No breaking change to a contract surface, **or** the break is called out
      below and intended.

Changes to any of these are breaking — describe + justify if touched:
`agent.yaml`/`project.yaml` schema · public CLI flags / `--json` shapes ·
`/api/v1` runtime API · storage schema/migrations · `MOVATE_*`/`MDK_*` env
vars · deploy behavior (bicep params, image-tag scheme, deploy modes).

- **Compat notes:**

## Failure modes considered

<!-- Enterprise AI fails operationally, not syntactically. Which of these
     apply, and how are they handled? -->

- API/timeout failure · embedding-model or schema drift · partial retrieval ·
  tracer/Langfuse unavailable · partial Azure deploy · duplicate jobs · …
- **Notes:**

## Tests

- [ ] Behavior + edge cases + failure modes covered by tests (written
      alongside, ideally first).
- [ ] `ruff check` + `ruff format --check` + `mypy src` + `pytest -m "not smoke"` pass.
- [ ] `python scripts/check_licenses.py --strict` passes (if deps changed).

## Dependencies

- [ ] No new dependency, **or** the new dep is justified below, permissively
      licensed, and (if heavy/optional) added to an opt-in `pyproject.toml` extra.
- **New deps + justification:**
