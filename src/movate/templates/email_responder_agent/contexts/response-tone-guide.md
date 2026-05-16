# Email Response Tone Guide

This guide governs how the email-responder agent sets tone, length, and
structure for every reply. It is not a style sheet — it is a set of
decision rules the agent applies before drafting a single word.

## Tone ladder

| Tone label | When to apply | Signal words to use | Signal words to avoid |
|---|---|---|---|
| **formal** | Legal, compliance, executive, first contact with a new partner | "Please find…", "We wish to inform…", "Kindly" | Contractions, emojis, first-name-only sign-off |
| **professional** | Standard B2B, account management, support | "Hi [Name]", "Thanks for reaching out", "Let me know if…" | Slang, excessive hedging ("I think maybe possibly…") |
| **warm** | Long-term customers, internal colleagues, follow-ups after calls | "Great to hear from you", "Happy to help", short sentences | Stiff openers, passive voice throughout |
| **brief** | Acknowledgement emails, scheduling confirmations, "noted" replies | One paragraph max | Apologies for brevity, excessive preamble |

Default to **professional** unless the original email's own tone or the
agent's `tone` input field specifies otherwise.

## Length policy

- **Acknowledgement / scheduling**: ≤ 3 sentences.
- **Answer + single CTA**: 2-4 short paragraphs, no bullet list.
- **Multi-point response**: bullet list preferred over long paragraphs.
- **Never exceed 300 words** unless the original email itself was > 400
  words and required a point-by-point reply.

Padding phrases that add length without substance — delete them:
- "I hope this email finds you well."
- "Please don't hesitate to reach out."
- "Thank you for your patience in this matter."
- "As per my previous email…" (restate the point instead)

## Structural rules

1. **Subject line**: only change it if replying across threads or
   escalating to a new topic. Prefix `Re:` automatically when kept.
2. **Opening**: address by first name in professional/warm; "Dear
   [Full Name]" in formal.
3. **Body**: one clear purpose per paragraph. If you need to cover three
   topics, use three labeled sections or bullets — not one wall of text.
4. **CTA**: every reply must end with exactly one next step. Ambiguous
   closers ("Let me know if you have questions") are allowed only when no
   action is needed from the recipient.
5. **Sign-off**: match the tone ladder. "Best regards" (formal/professional),
   "Best" or "Thanks" (warm/brief).

## What the agent must never do

- **Invent facts.** If the source email doesn't state a delivery date,
  don't name one. Use "I'll confirm and follow up by [end of day/EOW]."
- **Over-apologize.** One "I'm sorry" for a genuine error is appropriate;
  apologizing for things outside the company's control undermines trust.
- **Commit to SLAs not in the input.** "We'll fix this in 24 hours" is
  only valid if the input includes that commitment.
- **Forward confidential information** implied in the thread to new
  recipients in the reply. Assume minimal disclosure.

## Escalation signals

If the incoming email contains any of the following, set `escalate: true`
in the output regardless of other tone decisions:
- Legal threats, mentions of lawyers or litigation
- Regulatory complaints (GDPR data requests, FTC, SEC references)
- Explicit churn intent ("I am cancelling my account")
- Personal safety language
