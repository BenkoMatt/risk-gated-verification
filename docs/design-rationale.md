# Design rationale

## The problem is not “the model needs more thinking”

A drafting model that is fast enough to use all day will sometimes be wrong with full confidence. Increasing its reasoning budget helps with hard problems, but it does not give the model an independent view of whether a claim is true. The model is still reasoning from the same weights, the same context, and the same blind spots.

The useful separation is:

```text
maker:   produce a fast, useful draft
checker: try to falsify the draft, with evidence
tools:   decide disputes that can be checked
human:   approve actions that cannot be undone
```

That is the **maker–checker** pattern. Risk-gated verification is a concrete, low-friction implementation of it for a chat agent.

## Why not verify every turn?

Because most turns do not contain a checkable claim. A greeting, a naming brainstorm, a code explanation, or a design opinion does not get better by sending it to a second model. The cost is not only money:

- extra seconds of latency on every reply,
- extra context serialization,
- a second model's own failure modes,
- more quota burn during parallel work,
- more chances for a verifier to introduce noise into a good answer.

The system should spend verification where a wrong answer is expensive. That is the entire point of a **gate**.

## Why not delegate every question?

The tempting design is:

```text
every question
  → spawn a subagent to decide whether to verify
  → maybe spawn another subagent to verify
```

That is two round-trips to decide whether to spend one. It also makes the parent wait, burns a concurrency slot, and gives the triage agent the same context-serialization problem as any other child. A cheap typed classifier plus one direct verifier call is faster and easier to reason about.

Delegation still has a role: when the verifier must gather its own evidence, or when a non-interactive job can afford a longer independent investigation. It should be an explicit, high-consequence workflow — not the default gate.

## Why a decision model instead of only regex?

Regex is fast, deterministic, and easy to audit. It is also literal. It catches “client invoice” and “force-push,” but it misses a polite paraphrase or a short follow-up that inherits risk from the previous turn.

A small decision model handles the ambiguous middle:

- typed output: a choice among `low`, `medium`, `high`,
- short input: the turn plus a little recent history,
- short timeout: roughly half a second in the reference deployment,
- fallback: the regex verdict on any failure.

The important design choice is that the model **classifies risk**, not truth. It does not decide whether the answer is correct. It decides how much process the turn deserves.

Regex remains the fast path for obvious high-risk language. It is cheaper and more predictable than asking a model to rediscover “invoice = high.”

## Why medium risk is append-only

A verifier that can rewrite a medium-risk answer can silently make a good answer worse. The reviewer sees a bounded task, a bounded draft, and bounded evidence; it does not see the full conversational context or the operator's intent.

So medium risk keeps the draft and appends:

- the concrete issues that survived quote checking,
- a short suggested correction,
- what evidence is still missing.

The human sees both the original and the challenge. That is more useful than a confident rewrite.

High risk is different. A client-facing commitment, a security change, or an irreversible action is expensive enough that a corrected draft is worth the risk of a verifier rewrite — but it still carries a visible note when issues remain.

## Why the verifier must quote the draft

This was learned the hard way. An LLM reviewer can produce a plausible critique of a sentence that does not exist. It can also attribute facts from the prompt, prior context, or its own memory to the draft. The result looks rigorous and is useless.

The quote-membership rule makes the reviewer's output partly falsifiable:

```text
issue accepted  ⇔  issue contains a quoted substring
                   that literally appears in the draft
```

This does not prove the issue is correct. It does prove the reviewer is talking about text that exists. It also suppresses style nits and generic advice, because those rarely quote the draft.

If no issue survives, the review is treated as a pass. Silence is better than a fabricated warning.

## Why fail open?

A checker failure is not evidence that the draft is wrong. If the verifier times out or returns malformed JSON, hiding the answer would make the system less useful without making it more correct.

Fail-open is a deliberate trade:

- the draft remains available,
- the verification note may be absent,
- the system does not pretend a review happened.

For high-consequence actions, the separate approval gate still applies. Fail-open is not permission to send a client invoice unchecked; it is a rule about what the verification layer does when its own machinery breaks.

## Why not simply use a stronger model everywhere?

A stronger model can reduce errors, but it does not give you an independent check. It also changes the latency and quota profile of every turn, including the ones that never needed help.

The architecture is model-agnostic:

- use the fastest adequate model for drafting,
- use a different model for checking when the turn warrants it,
- use tools for facts that can be checked mechanically,
- use a human for decisions that cannot be undone.

That is more robust than betting the entire workflow on one model's self-assessment.

## The adoption bar

A verification layer should earn its place. In the reference deployment, the gate was kept because:

- low-risk turns incurred no verifier call,
- medium/high reviews found real unsupported claims,
- the quote check removed fabricated issues,
- failures preserved the draft,
- the extra latency was bounded by a short timeout and a 60-second hook budget.

It would be reasonable to remove or narrow the layer if:

- the classifier consistently misroutes ordinary work,
- the verifier's surviving issues are mostly noise after quote checking,
- the added latency is visible on turns that do not need review,
- the verifier cannot be given enough evidence to say anything useful.

Those are measurable conditions, not vibes. Measure them before expanding the gate.

## What this design is not

- It is not a formal proof of correctness.
- It is not a replacement for tests, source documents, or human approval.
- It is not a way to make a weak model omniscient.
- It is not a reason to send private material to a web search.
- It is not a per-question delegation framework.

It is a small, fail-open control loop that makes the expensive verification path automatic for the turns that need it.
