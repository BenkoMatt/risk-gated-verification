# Implementation walkthrough

This is the build order for a working Hermes plugin. The complete reference is in [`plugin/`](../plugin/). Read [`how-it-works.md`](how-it-works.md) first if you want the mechanics without the code.

## 0. Prerequisites

- A Hermes Agent install with the plugin hook API.
- A model provider available through Hermes' `ctx.llm.complete`.
- For the reference path: an Ollama Cloud key and access to `deepseek-v4.1-flash:1017`, `inkling-2-small`, and optionally `clef:27b`.
- Python 3.11+ only for the standalone tests.

The pure classifier and quote checker have no third-party dependencies. The Hermes hooks do.

## 1. Create the plugin directory

Hermes discovers user plugins under `~/.hermes/plugins/`. A profile can have its own plugin directory, but for a fleet-wide deployment the clean pattern is one real directory plus per-profile symlinks.

```bash
mkdir -p ~/.hermes/plugins/risk-gated-verification
cd ~/.hermes/plugins/risk-gated-verification
touch plugin.yaml __init__.py
```

Start with the manifest, because `hermes plugins doctor` reads it before it imports anything.

## 2. Write `plugin.yaml`

```yaml
name: risk-gated-verification
version: "0.1.0"
description: "Classify each turn, inject a verification policy, review medium/high-risk replies with an independent model, and require evidence after code edits."
author: "Caddy"
provides_hooks:
  - pre_llm_call
  - post_tool_call
  - transform_llm_output
  - pre_verify
config_schema:
  auto_review:
    type: bool
    default: true
  review_medium:
    type: bool
    default: true
  review_high:
    type: bool
    default: true
  use_decision_model:
    type: bool
    default: true
```

The hook names are the contract. A typo here produces a plugin that imports cleanly and does nothing.

## 3. Define the classifier

Start with the cheap, deterministic path. High-consequence language wins first, then moving facts and technical claims.

```python
import re

_HIGH_PATTERNS = (
    r"\b(client|customer|invoice|contract|legal|lawsuit|compliance|policy|terms)\b",
    r"\b(price|pricing|quote|refund|payment|billing|tax|salary|budget)\b",
    r"\b(security|vulnerab|exploit|password|credential|secret|token|auth|permission|access)\b",
    r"\b(deploy|production|prod|gateway|systemd|cron|database|migration|backup|restore)\b",
    r"\b(delete|drop|truncate|rm\s+-rf|force[- ]push|reset\s+--hard)\b",
)

_MEDIUM_PATTERNS = (
    r"\b(current|currently|latest|newest|today|this week|this month|release[sd]?|version)\b",
    r"\b(compare|comparison|benchmark|evaluate|research|source|verify|fact[- ]?check)\b",
    r"\b(api|sdk|config|configuration|install|upgrade|error|bug|fix|patch|test|lint|build)\b",
    r"\b(documentation|docs|manual|spec|specification|changelog|release notes)\b",
)

_HIGH_RE = re.compile("|".join(_HIGH_PATTERNS), re.IGNORECASE)
_MEDIUM_RE = re.compile("|".join(_MEDIUM_PATTERNS), re.IGNORECASE)

def classify(text: str) -> str:
    text = text or ""
    if _HIGH_RE.search(text):
        return "high"
    if _MEDIUM_RE.search(text):
        return "medium"
    return "low"
```

Add a small context resolver for follow-ups:

```python
_CONTEXT_RE = re.compile(
    r"^\s*(do it|do that|go ahead|proceed|continue|yes|no|ok|sure|please do|that one|this one)\b",
    re.IGNORECASE,
)

def _history_text(history):
    parts = []
    for item in (history or [])[-4:]:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            parts.append(f"{role}: {content.strip()[:800]}")
    return "\n".join(parts)
```

The key rule: **do not classify a bare “do it” in isolation.** Combine it with recent turns when the current message is short or matches the follow-up regex.

## 4. Add an optional decision-model adapter

The reference uses a typed decision endpoint. Keep it optional and time-boxed.

```python
import json
import os
import urllib.request

CLEF_ENDPOINT = os.environ.get(
    "CLEF_ENDPOINT", "https://ollama.com/v1/systemone"
)
CLEF_MODEL = os.environ.get("CLEF_MODEL", "clef:27b")
CLEF_TIMEOUT = 3.0

_CRITERIA = {
    "high": "Client-facing, money/legal/security, infrastructure, or irreversible action, even if the user says do not send.",
    "low": "No externally checkable claim or consequence.",
    "medium": "A factual or technical claim that should be checked before stating.",
}

def _clef_classify(text: str):
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not key:
        return None
    payload = {
        "model": CLEF_MODEL,
        "state": text[:8000],
        "questions": {
            "risk_tier": {
                "type": "choice",
                "instructions": (
                    "How much independent verification does this request need "
                    "before answering? High includes client-facing drafts, "
                    "invoices, contracts, pricing, configuration changes, and "
                    "security decisions even if they will not be sent yet."
                ),
                "criteria": _CRITERIA,
            }
        },
    }
    try:
        req = urllib.request.Request(
            CLEF_ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=CLEF_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        choice = (data.get("answers") or {}).get("risk_tier", {}).get("choice")
        return choice if choice in {"low", "medium", "high"} else None
    except Exception:
        return None
```

If the endpoint is unavailable, the caller keeps the regex result. Never make the decision model a hard dependency.

## 5. Implement `pre_llm_call`

This hook does four things: classify, store state, clear the evidence list, and inject policy.

```python
import threading

_CTX = None
_LOCK = threading.Lock()
_LAST_RISK = {}
_EVIDENCE = {}

def _policy_for(risk: str):
    if risk == "low":
        return None
    if risk == "high":
        return (
            "[Verification policy: HIGH RISK]\n"
            "Before answering: (1) verify every externally checkable claim "
            "against a tool, source, file, test, or command; (2) do not guess "
            "names, dates, prices, versions, paths, permissions, or legal/policy "
            "details; (3) run the relevant test/lint/build or dry-run before "
            "claiming success; (4) distinguish verified facts from assumptions. "
            "If evidence is unavailable, say exactly what is unverified. For a "
            "client-facing or irreversible action, present the evidence and wait "
            "for the operator's approval instead of claiming completion."
        )
    return (
        "[Verification policy: MEDIUM RISK]\n"
        "This turn contains factual or technical claims. Check volatile facts "
        "(versions, prices, dates, current status) with a tool or authoritative "
        "source before stating them; inspect local files before claiming their "
        "contents. Mark anything you could not verify."
    )

def _pre_llm_call(user_message="", session_id="", platform="", conversation_history=None, **_):
    history = _history_text(conversation_history)
    needs_context = bool(history) and (
        len((user_message or "").strip()) < 40
        or bool(_CONTEXT_RE.match(user_message or ""))
    )
    classify_input = (
        f"Recent conversation:\n{history}\nCurrent request: {user_message}"
        if needs_context else user_message
    )

    risk = classify(classify_input)
    use_clef = True
    try:
        use_clef = bool(_CTX.get_config("use_decision_model", True))
    except Exception:
        pass

    if use_clef and risk != "high" and (
        len((user_message or "").strip()) >= 24 or needs_context
    ):
        clef_risk = _clef_classify(classify_input)
        if clef_risk:
            risk = clef_risk

    with _LOCK:
        key = session_id or "default"
        _LAST_RISK[key] = {
            "risk": risk,
            "message": classify_input,
            "platform": platform,
        }
        _EVIDENCE[key] = []
        if len(_LAST_RISK) > 256:
            for old in list(_LAST_RISK)[:-128]:
                _LAST_RISK.pop(old, None)
                _EVIDENCE.pop(old, None)

    policy = _policy_for(risk)
    return {"context": policy} if policy else None
```

The high-risk regex fast path skips the network call. That is deliberate: obvious high-risk turns should not wait for a classifier to rediscover the obvious.

## 6. Implement `post_tool_call`

Keep the evidence bounded. The verifier does not need a full transcript; it needs the authoritative results from this turn.

```python
def _post_tool_call(tool_name="", args=None, result=None, session_id="", **_):
    if result is None:
        return
    if not isinstance(result, (str, bytes)):
        result = str(result)
    if isinstance(result, bytes):
        result = result.decode("utf-8", "replace")
    payload = (
        f"[{tool_name}]\n"
        f"args={str(args)[:800]}\n"
        f"result={str(result)[:2400]}"
    )
    with _LOCK:
        bucket = _EVIDENCE.setdefault(session_id or "default", [])
        bucket.append(payload)
        del bucket[:-8]
```

Passing tool evidence is what lets the reviewer distinguish “unsupported because the model guessed” from “supported by the command the model just ran.”

## 7. Implement the verifier call

The verifier should be a different model, and it should have a hostile brief.

```python
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "verdict": {"type": "string", "enum": ["pass", "revise", "block"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "corrected_answer": {"type": ["string", "null"]},
        "evidence_needed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["risk", "verdict", "issues"],
}

def _extract_json(text):
    import re, json
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except (TypeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None

def _review_with_inkling(user_message, answer, risk, evidence=None):
    if _CTX is None:
        return None

    evidence_block = ""
    if evidence:
        evidence_block = (
            "\n\nTOOL EVIDENCE (authoritative, from this turn):\n"
            + "\n".join(evidence[-8:])[:6000]
        )

    prompt = (
        "You are an independent verifier. The user asked the task below, and "
        "another model produced the proposed answer. Your job is to find defects, "
        "not to praise.\n\n"
        "Rules:\n"
        "- Check every factual, technical, legal, financial, configuration, or "
        "client-facing claim.\n"
        "- Only report defects that affect correctness, safety, or client "
        "commitments. Ignore tone, style, emoji, and formatting unless they "
        "change meaning.\n"
        "- Treat the tool evidence as authoritative; do not flag a claim as "
        "unverifiable when the evidence supports it.\n"
        "- If you cannot verify a claim from the provided text, evidence, or "
        "general knowledge, say so.\n"
        "- Do not invent sources. Do not silently rewrite style.\n"
        "- Return JSON only with keys: risk (low/medium/high), verdict "
        "(pass/revise/block), issues (array of concrete strings), "
        "corrected_answer (string or null), evidence_needed (array of strings).\n"
        "- Use verdict=revise only when you provide a corrected answer; use "
        "block when the draft contains a critical unsupported claim and no safe "
        "correction is possible.\n\n"
        f"TASK:\n{user_message[:6000]}\n\n"
        f"PROPOSED ANSWER:\n{answer[:12000]}"
        f"{evidence_block}"
    )

    try:
        result = _CTX.llm.complete(
            [{"role": "user", "content": prompt}],
            model=os.environ.get("RISK_GATE_VERIFIER_MODEL", "inkling-2-small"),
            temperature=0.1,
            max_tokens=1400,
            timeout=28,
            purpose="risk-gated-verification",
        )
        parsed = _extract_json(getattr(result, "text", "") or "")
        if parsed is None:
            return None
        return _sanitize_review(answer, parsed)
    except Exception:
        return None
```

The `temperature=0.1` is for consistency, not creativity. The verifier is not a writer.

## 8. Enforce the quote contract

```python
_QUOTE_RE = re.compile(r'[“"`](.{6,}?)[”"`]')

def _filter_quoted_issues(answer, issues):
    if not isinstance(issues, list):
        return []
    normalized_answer = " ".join(str(answer).split()).lower()
    kept = []
    for issue in issues:
        if not isinstance(issue, str):
            continue
        quotes = _QUOTE_RE.findall(issue)
        if any(
            " ".join(q.split()).lower() in normalized_answer
            for q in quotes
        ):
            kept.append(issue)
    return kept

def _sanitize_review(answer, parsed):
    parsed["issues"] = _filter_quoted_issues(answer, parsed.get("issues"))
    if not parsed["issues"]:
        parsed["verdict"] = "pass"
        parsed["corrected_answer"] = None
        parsed["evidence_needed"] = []
    return parsed
```

This is not cosmetic. It is the difference between a review you can act on and a review that invents a sentence and then grades it.

## 9. Implement `transform_llm_output`

```python
def _setting(name, default):
    try:
        return _CTX.get_config(name, default) if _CTX is not None else default
    except Exception:
        return default

def _format_issues(issues, evidence, source="inkling"):
    label = "Inkling review" if source == "inkling" else "Delegated verifier"
    lines = ["", "---", f"**Verification note ({label})**"]
    if issues:
        lines.append("")
        for item in issues[:8]:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")
    if evidence:
        lines.append("")
        lines.append(
            "Evidence needed: "
            + "; ".join(str(e).strip() for e in evidence[:6] if str(e).strip())
        )
    return "\n".join(lines)

def _transform_llm_output(response_text="", session_id="", model="", **_):
    if not response_text:
        return None

    with _LOCK:
        state = dict(_LAST_RISK.get(session_id or "default") or {})
    risk = str(state.get("risk") or "low")
    if risk == "low":
        return None

    if not _setting("auto_review", True):
        return None
    if risk == "medium" and not _setting("review_medium", True):
        return None
    if risk == "high" and not _setting("review_high", True):
        return None

    evidence = list(_EVIDENCE.get(session_id or "default") or [])
    review = _review_with_inkling(
        str(state.get("message") or ""),
        response_text,
        risk,
        evidence=evidence,
    )
    if not review:
        return None

    verdict = str(review.get("verdict") or "").strip().lower()
    issues = review.get("issues") if isinstance(review.get("issues"), list) else []
    needed = (
        review.get("evidence_needed")
        if isinstance(review.get("evidence_needed"), list) else []
    )
    corrected = review.get("corrected_answer")

    if verdict == "pass":
        return None
    if verdict == "revise" and isinstance(corrected, str) and corrected.strip():
        if risk == "high":
            suffix = _format_issues(issues, needed) if (issues or needed) else ""
            return corrected.strip() + suffix
        # Medium risk: keep the draft, surface the challenge.
        return (
            response_text
            + (_format_issues(issues, needed) or "")
            + f"\n\nSuggested correction: {corrected.strip()}"
        )
    if verdict == "block":
        prefix = "⚠️ **Verification blocked this answer.**\n"
        note = _format_issues(issues, needed)
        return prefix + response_text + ("\n" + note if note else "")
    if issues or needed:
        return response_text + _format_issues(issues, needed)
    return None
```

The asymmetry is the important part. Medium risk is append-only; high risk may replace.

## 10. Add `pre_verify` for code edits

```python
def _pre_verify(coding=False, attempt=0, changed_paths=None, **_):
    if not coding or attempt > 0:
        return None
    paths = [p for p in (changed_paths or []) if isinstance(p, str)]
    if not paths:
        return None
    return {
        "action": "continue",
        "message": (
            "Before finishing, run the relevant tests/linter/build for the changed "
            "code and report the exact command and result. If no test applies, "
            "state why and describe the manual check. Do not claim the change is "
            "verified without fresh evidence. Changed paths: "
            + ", ".join(paths[:12])
        ),
    }
```

One nudge per turn. A loop of “are you sure?” wastes tokens and annoys the operator.

## 11. Register the hooks

```python
def register(ctx):
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("pre_verify", _pre_verify)
```

`ctx` is the only host object the plugin keeps. It provides `get_config` and `llm.complete`; the hook signatures receive the rest.

## 12. Configure Hermes

Add to the profile config:

```yaml
plugins:
  hook_callback_timeout: 60
  entries:
    risk-gated-verification:
      enabled: true
      llm:
        allow_model_override: true
        allowed_models:
          - inkling-2-small
      settings:
        auto_review: true
        review_medium: true
        review_high: true
        use_decision_model: true
```

Why `allow_model_override`? The verifier deliberately calls a different model than the drafter. Without the allow-list, the host may refuse the override.

Why 60 seconds? The direct review timeout is 28 seconds. The hook budget must be larger than the review timeout plus overhead. If you test a delegated verifier, budget 180 seconds and expect a much worse tail.

## 13. Enable, doctor, and test

```bash
hermes plugins doctor risk-gated-verification
hermes plugins enable risk-gated-verification
hermes plugins list | grep risk-gated
```

Test three turns in a **new chat**:

| Prompt | Expected |
|---|---|
| `Give me three names for a coffee shop.` | Direct answer, no verification note, no extra model call |
| `What is the latest stable version of Python?` | Draft plus a verification note if the version claim is unsupported; otherwise a clean pass |
| `Draft a client invoice for last month.` | High-risk policy; adversarial review; corrected answer or visible issues |

Then check the logs:

```bash
grep "risk-gated-verification" ~/.hermes/logs/gateway.log | tail -n 20
```

If the plugin loads but never reviews, check in this order:

1. Does `plugin.yaml` list all four hooks?
2. Did you start a new chat after enabling? Existing sessions can cache old wiring.
3. Is the turn actually medium/high? A short low-risk prompt will not review.
4. Is `auto_review` true, and is the relevant `review_medium` / `review_high` true?
5. Does `ctx.get_config("auto_review", True)` work for your host version? Read keys directly, not a nested `settings` object.
6. Did the verifier time out? Search the log for `verifier unavailable` or `left answer unchanged`.

## 14. Deploy across profiles

If Hermes has multiple profiles, keep one canonical plugin directory and symlink it into each profile:

```bash
PLUGIN="$HOME/.hermes/plugins/risk-gated-verification"
for p in default daily research reviewer security webdev work; do
  mkdir -p "$HOME/.hermes/profiles/$p/plugins"
  ln -sfn "$PLUGIN" "$HOME/.hermes/profiles/$p/plugins/risk-gated-verification"
done
```

Then add the config block to each profile's `config.yaml`. A symlink alone enables discovery, not the settings. Verify each profile:

```bash
for p in default daily research reviewer security webdev work; do
  echo "== $p"
  hermes -p "$p" plugins list | grep risk-gated
done
```

Do not copy the code seven times. One source of truth, seven symlinks, seven config blocks.

## 15. Observability

At minimum, log:

- the classified tier and whether it came from regex or Clef,
- whether a review ran, its verdict, and whether issues survived quote checking,
- verifier latency,
- fallback events (timeout, non-JSON, no API key).

A compact debug line is enough:

```text
risk=medium source=clef review=revise kept=1/2 latency_ms=1840
```

Never log the full draft or client content in a shared log. Log identifiers and counts, not secrets.

## 16. Production checklist

- [ ] Manifest lists all four hooks
- [ ] High-risk regex runs before any network call
- [ ] Short follow-ups use recent history
- [ ] Decision model has a short timeout and a regex fallback
- [ ] Low-risk turns make no verifier call
- [ ] Evidence trail is bounded and truncated
- [ ] Verifier is a different model from the drafter
- [ ] Verifier returns a strict JSON shape
- [ ] Every surviving issue quotes the draft
- [ ] Medium risk is append-only
- [ ] High risk may replace only on `revise`
- [ ] Verifier failure preserves the draft
- [ ] Code edits trigger a test-evidence nudge
- [ ] New sessions are tested after enabling
- [ ] Each profile has its own config block
- [ ] Logs contain counts, not private content

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin listed but no review | Old session, or turn classified low | New chat; use a medium/high prompt |
| `verifier unavailable` | Timeout, missing key, or model not allowed | Check key, model allow-list, timeout |
| Review note with no issues | Old code appended on pass | Only append on `revise` / `block`, or when issues survive |
| Reviewer invents a quote | No quote-membership check | Add `_filter_quoted_issues` |
| Medium answer gets silently rewritten | Wrong verdict handling | Keep medium append-only |
| Config change has no effect | Session cached, or settings read from wrong path | New chat; read config keys directly |
| Plugin loaded twice per turn | Duplicate profile plugin dirs / stale gateway | Use symlinks; one gateway; check logs |
