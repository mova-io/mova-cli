# Citation Style Guide

This guide defines how the research-agent references sources, handles
conflicts, and decides what to include in each output field.

## Citation format

Sources are numbered by their position in the `input.sources` array,
starting at 1. Always use the format `[N]` inline — never footnotes,
URLs, or author-year style.

- Single source: `[1]`
- Multiple corroborating sources: `[1,3]` (comma-separated, no spaces,
  ascending order)
- Contradiction between two sources: note both in `disagreements` and
  reference them as `[2] vs [4]`

Every factual claim in `executive_summary` and every `key_point.claim`
**must** cite at least one source. Claims with no citation are fabrications
— do not include them.

## What counts as a citation

A citation is valid only when the claim is directly derivable from the
source text provided in `input.sources[N].content`. Do not cite a source
because its title sounds relevant — you must be able to point to specific
text in the content that supports the claim.

If a source's title suggests relevance but the provided content does not
actually support a claim, omit the claim or move it to `open_questions`.

## executive_summary rules

- 3–5 sentences, no more, no less.
- Every sentence must have at least one citation.
- The first sentence should state the main conclusion across all sources.
- Do not introduce new claims in the summary that don't appear in
  `key_points`.
- Do not use weasel words ("some experts believe," "it may be the case").
  State what the sources say, attributed.

## key_points rules

- One sentence per point. No multi-sentence claims.
- If you cannot express the claim in one sentence without losing essential
  meaning, split it into two `key_point` entries.
- Prefer specific, falsifiable claims over general observations.
  - Good: "Temperatures in the region rose 1.4°C between 1990 and 2020 [2]."
  - Bad: "Climate change has impacted the region [2]."

## disagreements rules

- List only direct contradictions, not differences in emphasis.
- Format: state Source A's claim [N], then Source B's contradicting
  claim [M].
- Example: "Source [1] states the compound is non-toxic at doses below
  50mg/kg; Source [3] reports adverse effects at 30mg/kg in animal studies."
- If all sources agree, return an empty list `[]`.

## open_questions rules

- List questions that a reader would reasonably ask after reading the
  summary but that none of the provided sources answer.
- Be specific: "What is the failure rate under high-concurrency load?"
  not "More research is needed."
- If the sources fully address the topic, return `[]`.

## What the agent must NOT do

- **Hallucinate sources.** Only cite source indices 1 through N where N
  is the number of entries in `input.sources`.
- **Synthesize beyond the sources.** If the topic requires outside
  knowledge to reach a conclusion, note it as an `open_question`.
- **Editorialize.** The agent's opinion does not appear anywhere in
  the output. Only source-attributed claims.
- **Omit conflicts.** Cherry-picking the majority view and hiding a
  minority source that disagrees is a reportorial error, not a
  synthesis judgment.
