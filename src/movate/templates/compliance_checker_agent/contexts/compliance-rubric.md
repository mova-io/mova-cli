# Compliance Checking Rubric

This rubric tells the compliance-checker agent how to evaluate text against
a ruleset, assign severity, and produce findings that a human reviewer can
act on without re-reading the original.

## Severity levels

Every finding must use exactly one of these severity values:

| Severity | Definition | Examples |
|---|---|---|
| `critical` | Violates a hard legal requirement or creates direct regulatory exposure. Do not ship without remediation. | GDPR data subject right denial, HIPAA PHI disclosure, securities forward-looking statement without safe-harbor disclaimer |
| `high` | Violates stated policy with a plausible harm pathway. Must be fixed before publication. | Claim not substantiated in the underlying data, prohibited endorsement language, missing required disclosure |
| `medium` | Policy gap or ambiguous wording that a regulator or auditor could reasonably flag. Should be fixed. | Passive construction that obscures data use, technically true but misleading comparison |
| `low` | Style or convention issue. No regulatory exposure, but clean-up improves auditability. | Inconsistent terminology, deprecated legal boilerplate that is still technically valid |

When in doubt between two severity levels, assign the higher one and
explain the uncertainty in `suggested_fix`.

## What a finding must contain

Each finding in the output array requires:

- **`rule_ref`**: the exact rule identifier from the input ruleset
  (e.g. `GDPR_Art13`, `FTC_Endorsement_Guide_§255.1`). If the ruleset
  uses free-text rule names, reproduce them verbatim. Do not paraphrase.
- **`excerpt`**: the exact verbatim text from the input that triggered
  the finding. Keep it to the minimal self-contained phrase — not the
  whole paragraph.
- **`explanation`**: one to three sentences on why this excerpt violates
  the cited rule.
- **`severity`**: one of `critical`, `high`, `medium`, `low`.
- **`suggested_fix`**: a rewritten version of the excerpt that would
  satisfy the rule, or a specific instruction if a rewrite is not
  possible (e.g. "Remove this sentence — no compliant phrasing exists
  for this claim without supporting data").

## Ordering and deduplication

- Output findings in descending severity order (`critical` first).
- If the same rule is triggered multiple times by different excerpts,
  create one finding per excerpt — do not merge them.
- If the same excerpt triggers two different rules, create two separate
  findings.

## What the agent must NOT do

- **Invent rule references.** Only cite rules that appear in the input
  ruleset. If no matching rule exists for a genuine concern, add it to
  `overall_assessment` as a free-form note — not as a finding.
- **Rewrite without being asked.** The `suggested_fix` field should fix
  only the compliance issue — do not restructure, expand, or improve
  the surrounding text for other reasons.
- **Flag ambiguity as a violation.** Ambiguous phrasing that could be
  read multiple ways is a `medium` or `low` finding only if a specific
  rule requires clarity in that context.
- **Apply jurisdictions not in scope.** Only check against rules in the
  provided ruleset. Do not volunteer findings under CCPA if the ruleset
  covers only GDPR, even if a violation is apparent.

## overall_assessment

Summarize in 2–4 sentences:
1. How many findings at each severity level.
2. The highest-risk area (which rule category generates the most
   critical/high findings).
3. Whether the text is suitable for publication as-is (and if not, what
   must change before it is).

If there are zero findings, state that clearly: "No violations found
against the provided ruleset." Do not manufacture findings to appear
thorough.
