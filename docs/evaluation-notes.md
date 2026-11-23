# Evaluation notes

These notes are from a working deployment, not a controlled public benchmark. They explain why the design looks the way it does and which failure modes were observed.

## What was measured

### Decision-model latency

On the reference host, the typed decision-model endpoint returned in roughly **0.5 seconds**:

| Model | Approx. latency | Notes |
|---|---:|---|
| `clef-flash` | ~0.47 s | Cheaper, but misclassified a client-invoice draft as low at 0.25 confidence |
| `clef:27b` | ~0.51 s | Correctly returned high at 0.95 for the invoice and 0.82 for a force-push case |

The lesson was not “use the bigger model for everything.” It was: **use a decision model that is calibrated for the consequence of a miss.** A false low on a client document is expensive; 40 ms of extra classifier latency is not.

### Verifier cost

A direct review typically consumed roughly **4k–9.5k tokens per reviewed turn**, depending on draft and evidence size. That is why `review_medium` is the main cost switch: high-risk review is rare and justified; medium-risk review happens often enough to dominate usage.

The direct review timeout was set to **28 seconds**, with a **60-second hook budget** to leave room for scheduling and response handling. A 30-second hook budget produced avoidable timeouts; 60 seconds was stable in this deployment.

### Review behavior

The verifier was useful when it received the actual tool evidence. In one live test it caught an unsupported “stale cached v0.33.3” claim in a draft. The original draft was preserved and the issue was appended as a review note, which is the intended medium-risk behavior.

The verifier was not useful when it was allowed to return free-form prose. It produced plausible criticism that did not correspond to the draft. Two fixes followed:

1. **Strict JSON contract.** The reviewer must return `risk`, `verdict`, `issues`, `corrected_answer`, and `evidence_needed`.
2. **Quote membership.** Every issue must contain a quoted substring that literally appears in the draft. Issues without a matching quote are dropped.

After quote checking, fabricated issues stopped reaching the user. If no issue survives, the review passes and no note is appended.

## The delegated verifier experiment

A cron/Kanban variant launched a web-only Inkling child before delivery. It was tested end-to-end and produced a verification note, but failed in two ways:

- The child returned **prose instead of the JSON contract** (`len=2784` in the observed run).
- It still **fabricated quotes** that were not present in the draft.

The variant was disabled everywhere. The direct in-process review replaced it. This is why the reference repository does not include a delegated verifier: the simpler path was more reliable, faster, and easier to bound.

If you need a separate evidence-gathering agent for a high-consequence task, build it as an explicit workflow with its own timeout and output contract. Do not make it the default gate for every turn.

## Why the quote check was necessary

An LLM reviewer can generate a critique of a sentence the maker never wrote. The issue sounds specific, which makes it more dangerous than a vague warning. The quote check converts the reviewer's output into a claim that can be mechanically tested against the draft:

```python
quotes = re.findall(r'[“"`](.{6,}?)[”"`]', issue)
accepted = any(normalize(q) in normalize(answer) for q in quotes)
```

This does not prove the issue is correct. It proves the reviewer is talking about text that exists. That is enough to remove the most common failure mode.

## Why medium risk is not rewritten

A verifier sees a bounded task, a bounded draft, and bounded evidence. It does not see the full conversation or the operator's intent. Letting it silently replace a medium-risk answer risks degrading a correct answer with a confident but context-poor rewrite.

So medium risk keeps the draft and appends the challenge. High risk may replace on an explicit `revise`, because the consequence of a wrong client/security/config answer is higher.

## Why the system fails open

Observed failures included:

- verifier request timeouts,
- non-JSON responses,
- missing decision-model responses,
- a hook budget that was too short.

In every case the correct behavior was to keep the draft and report the verifier failure in logs, not to hide the answer. A checker failure is not evidence that the draft is wrong.

## What these notes do not claim

- These are not benchmark scores for the drafting or verifier models.
- The token and latency numbers are deployment observations, not guarantees.
- A `pass` verdict is not proof of truth; it means no quoted defect survived.
- The system does not replace tests, source documents, or human approval.
- The risk tiers are heuristics. A misclassified turn is always possible.

## How to evaluate your own deployment

Use three prompt classes and check both the answer and the logs:

1. **Low:** a casual request. Expect no verifier call and no latency spike.
2. **Medium:** a volatile factual claim. Expect either a clean pass or a quoted, actionable note.
3. **High:** a client/money/security/config request. Expect a strict policy, a review, and evidence or an explicit unverified statement.

Then measure:

- verifier invocation rate by tier,
- median and p95 review latency,
- percentage of issues that survive quote checking,
- percentage of surviving issues the operator judges useful,
- cases where a correct draft was made worse.

Narrow or disable the gate if the surviving issues are mostly noise or the latency is visible on turns that did not need review. Expand it only when the evidence says the gate is catching real defects.
