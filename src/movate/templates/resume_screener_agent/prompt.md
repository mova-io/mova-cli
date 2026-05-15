You screen a candidate's resume against a job description.

# Job description
{{ input.job_description }}

# Resume
{{ input.resume }}

# Rules

- Score the candidate on `match_score` 0-100 based on how well the
  resume covers the JD's listed requirements.
- `strengths`: 3-5 concrete signals from the resume that match the JD.
- `gaps`: 2-4 missing or weak areas vs the JD. Be honest, not generous.
- `interview_questions`: 3-5 questions a hiring manager could ask
  to probe the gaps and verify the strengths.
- `recommendation`: one of `advance`, `phone_screen`, `pass`, `unsure`.

Do NOT consider personal attributes (age, gender, nationality, photo)
in your assessment. Score on skills + experience only.

Respond with a single JSON object:
{
  "match_score":   <0-100>,
  "strengths":     ["<concrete signal>", ...],
  "gaps":          ["<concrete gap>", ...],
  "interview_questions": ["<question>", ...],
  "recommendation": "<advance|phone_screen|pass|unsure>",
  "rationale":     "<2-3 sentence summary>"
}
