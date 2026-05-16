# Resume Screening Rubric

This rubric tells the resume-screener agent how to evaluate candidates,
score them, and produce actionable hiring recommendations. It is designed
to surface fit signals — not to make hiring decisions on behalf of the
human recruiter.

## The four scoring dimensions

Score each dimension 1–5 based on evidence in the resume only.

### 1. Skills match

Compare the JD's required and preferred skills against the resume.

| Score | Meaning |
|---|---|
| 5 | All required skills present; most preferred skills present |
| 4 | All required skills present; few preferred skills missing |
| 3 | Most required skills present; gaps are learnable (adjacent domain) |
| 2 | Key required skills missing; candidate would need significant ramp |
| 1 | Core required skills absent; no adjacent evidence of transferability |

Only score on **stated** skills and experience — do not infer skills from
job titles alone. "Led data platform migrations" supports a database
skill claim; "Software Engineer at Google" does not.

### 2. Experience depth

| Score | Meaning |
|---|---|
| 5 | Years + scope significantly exceed JD requirements; has owned similar problems |
| 4 | Meets JD requirements comfortably; one dimension (years OR scope) exceeds |
| 3 | Meets requirements; nothing stands out as exceptional |
| 2 | Below requirements on years OR scope, but not both |
| 1 | Substantially below requirements on both years and scope |

Experience depth is not a seniority filter — a 2-year candidate can
score 5 on a junior role.

### 3. Trajectory

Does the career arc suggest growth relevant to this role?

| Score | Meaning |
|---|---|
| 5 | Clear upward trajectory in responsibility and scope; consistent growth |
| 4 | General upward trend with minor plateaus |
| 3 | Lateral moves; stable but not advancing |
| 2 | Mixed signals; unexplained gaps or regression in scope |
| 1 | Downward trajectory or history inconsistent with this role's demands |

Do not penalize career changes — penalize unexplained gaps that the
resume does not account for.

### 4. Relevant achievements

Did the candidate quantify outcomes that relate to what this role will own?

| Score | Meaning |
|---|---|
| 5 | ≥ 3 specific, quantified achievements directly relevant to the JD |
| 4 | 2 quantified achievements or ≥ 3 relevant but unquantified |
| 3 | 1 quantified achievement, or achievements relevant but general |
| 2 | No quantification; achievements described generically |
| 1 | No achievements stated — only responsibilities listed |

## Composite score → recommendation

Average the four dimension scores (sum ÷ 4, one decimal place).

| Average | Recommendation |
|---|---|
| 4.5–5.0 | **`strong_yes`** — Advance to interview immediately |
| 3.5–4.4 | **`yes`** — Advance; note any gaps for interview focus |
| 2.5–3.4 | **`maybe`** — Screen call to clarify gaps before advancing |
| 1.5–2.4 | **`no`** — Does not meet requirements; archive |
| 1.0–1.4 | **`strong_no`** — Significant mismatch |

## Strengths and gaps

- **strengths**: List 2–4 concrete evidence-based strengths from the
  resume that are directly relevant to the JD. Each strength must cite
  a specific claim from the resume.
- **gaps**: List gaps that a hiring manager would likely raise in
  debrief. Only list material gaps (dimension scores of 1–2), not
  minor preferences.

Do not pad strengths or soften gaps. The recruiter needs honest signal,
not reassurance.

## Interview questions

Generate 3–5 questions that probe:
1. The highest-uncertainty dimension (lowest score).
2. Any resume claims that seem exceptional but lack supporting detail.
3. One behavioral question testing the core skill the JD emphasizes most.

Questions must be specific to this candidate and JD — not generic
"tell me about a time you…" templates. Bad: "Describe a time you
handled conflict." Better: "Your resume mentions leading a 12-person
cross-functional migration to microservices at Acme — how did you
handle the three teams that were blocked on the legacy API dependency?"

## Bias guard

Before finalizing scores, verify that no score was influenced by:
- School name, alma mater prestige, or absence of a degree when the
  JD doesn't require one.
- Employer brand (FAANG, startup vs. enterprise) rather than stated
  accomplishments.
- Name, location, or any demographic signal.
- Employment gaps without evidence that those gaps degraded relevant skills.

If you catch any of these influencing a score, recalibrate to evidence only.
