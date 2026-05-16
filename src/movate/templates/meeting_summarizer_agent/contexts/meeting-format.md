# Meeting Summary Format Guide

This guide defines what the meeting-summarizer agent captures, how it
structures the output, and what it must not invent.

## Core principle: transcript-faithful

Every item in the output must be traceable to something explicitly said
in the transcript. Summaries based on inference ("they probably agreed
to X") belong in `open_items`, not `decisions` or `action_items`.

## decisions

A decision was made if the transcript shows explicit agreement or
approval — a chair, lead, or group saying "we'll do X," "agreed," "let's
go with Y," or the equivalent.

Rules:
- State the decision in one sentence, past tense.
- Do not include the rationale unless it is critical to understanding
  the decision (then append it as a short parenthetical).
- Do not include decisions that were proposed but not confirmed.
- If a proposed decision was deferred to a later meeting, put it in
  `open_items`, not here.

## action_items

An action item has three required parts: **owner**, **action**, **due**.

- **owner**: the name of the person who explicitly accepted or was
  assigned the task. If the transcript says "someone should…" with no
  named owner, the item goes to `open_items` with a note that an owner
  needs to be assigned.
- **action**: an imperative verb phrase describing the deliverable.
  "Draft the Q3 forecast model" not "Q3 forecast."
- **due**: the date or meeting milestone stated in the transcript
  (`"by Friday"`, `"before next standup"`, `"EOQ"`). If no due date
  was stated, set `due: "unspecified"` — do not infer one.

## blockers

A blocker is an obstacle a participant explicitly named as preventing
progress on a current task. Criteria:
- Must be currently blocking — not a past problem that was resolved
  in the same meeting.
- Include who is blocked, what they're blocked on, and what (if anything)
  was said about resolution.
- Do not include general risks or concerns as blockers.

## follow_ups

Follow-ups are items that require attention before the next meeting but
are not action items with a named owner. Examples: questions sent to
external parties, decisions waiting on data, approvals in flight.

## participants

List only participants who spoke during the meeting. Attendees who were
present but silent should not appear. Use the names as stated in the
transcript (do not normalize to full names unless the transcript does).

## What the agent must NOT do

- **Fabricate owners.** If no one claimed an action, do not guess who
  should own it.
- **Smooth over conflict.** If two participants disagreed without
  resolution, record the disagreement in `open_items` — not a fabricated
  consensus.
- **Summarize pre-meeting context.** Only summarize what was said in
  this transcript. Prior meeting context mentioned in passing should not
  be restated as if it is a new decision.
- **Change tense to make things sound decided.** "We might explore option
  B" is an open item, not a decision.
- **Add meeting meta-commentary.** Do not note whether the meeting ran
  long, whether participants seemed engaged, or other observational
  editorials.

## Length guidelines

- **decisions**: 1 sentence each; ≤ 8 items (more usually indicates
  over-splitting).
- **action_items**: as many as the transcript supports; no artificial cap.
- **blockers**: only what is genuinely blocking; prefer 0–3.
- **follow_ups**: brief bullet phrases, not full sentences.
- **summary** (top-level field): 2–4 sentences covering the meeting's
  main purpose and outcome. Written last, from the other fields — not
  independently composed.
