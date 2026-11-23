"""Risk-gated verification for Hermes.

A small, fail-open control loop around an LLM agent:

1. ``pre_llm_call`` classifies the turn and injects a verification policy.
2. ``post_tool_call`` records a bounded evidence trail from the turn.
3. ``transform_llm_output`` asks a different model to review medium/high-risk
   drafts, accepts only quote-backed issues, and handles the verdict.
4. ``pre_verify`` nudges once for fresh test/lint/build evidence after code edits.

The reference deployment uses DeepSeek V4.1 Flash for drafting, Clef for risk
classification, and Inkling 2 Small for verification. The model names are
environment-overridable; the host's ``ctx.llm.complete`` interface does the rest.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CTX: Any = None
_LOCK = threading.Lock()
_LAST_RISK: dict[str, dict[str, Any]] = {}
_EVIDENCE: dict[str, list[str]] = {}

VERIFIER_MODEL = os.environ.get("RISK_GATE_VERIFIER_MODEL", "inkling-2-small")
VERIFIER_TIMEOUT = float(os.environ.get("RISK_GATE_VERIFIER_TIMEOUT", "28"))
VERIFIER_MAX_TOKENS = int(os.environ.get("RISK_GATE_VERIFIER_MAX_TOKENS", "1400"))
CLEF_MODEL = os.environ.get("CLEF_MODEL", "clef:27b")
CLEF_ENDPOINT = os.environ.get("CLEF_ENDPOINT", "https://ollama.com/v1/systemone")
CLEF_TIMEOUT = float(os.environ.get("CLEF_TIMEOUT", "3.0"))
MAX_EVIDENCE = 8

_DEBUG = os.environ.get("RISK_GATE_DEBUG", "").lower() in {"1", "true", "yes", "on"}

# High-consequence words: mistakes here are expensive, external, or hard to undo.
_HIGH_PATTERNS = (
    r"\b(client|customer|invoice|contract|legal|lawsuit|compliance|policy|terms)\b",
    r"\b(price|pricing|quote|refund|payment|billing|tax|salary|budget)\b",
    r"\b(security|vulnerab|exploit|password|credential|secret|token|auth|permission|access)\b",
    r"\b(deploy|production|prod|gateway|systemd|cron|database|migration|backup|restore)\b",
    r"\b(delete|drop|truncate|rm\s+-rf|force[- ]push|reset\s+--hard)\b",
    r"\b(send|email|publish|post|submit|file|approve|sign)\b.*\b(client|customer|county|boss|team)\b",
    r"\b(medical|diagnosis|prescription|dosage|legal advice)\b",
)

# Medium: facts that move, technical claims, and anything that reads like an audit.
_MEDIUM_PATTERNS = (
    r"\b(current|currently|latest|newest|today|this week|this month|release[sd]?|version)\b",
    r"\b(compare|comparison|benchmark|evaluate|research|source|verify|fact[- ]?check)\b",
    r"\b(api|sdk|config|configuration|install|upgrade|error|bug|fix|patch|test|lint|build)\b",
    r"\b(documentation|docs|manual|spec|specification|changelog|release notes)\b",
    r"\b(who|when|where|which|how many|how much)\b.*\?",
)

_HIGH_RE = re.compile("|".join(_HIGH_PATTERNS), re.IGNORECASE)
_MEDIUM_RE = re.compile("|".join(_MEDIUM_PATTERNS), re.IGNORECASE)
_TRIVIAL_RE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening)|how are you)\b",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(
    r"^\s*(do it|do that|go ahead|proceed|continue|yes|no|ok|sure|please do|that one|this one|sounds good)\b",
    re.IGNORECASE,
)

_QUOTE_RE = re.compile(r'[“"`](.{6,}?)[”"`]')

_CLEF_CRITERIA = {
    "high": (
        "Client-facing, money/legal/security, infrastructure, or irreversible "
        "action, even if the user says do not send."
    ),
    "low": "No externally checkable claim or consequence.",
    "medium": "A factual or technical claim that should be checked before stating.",
}


def _debug(event: str, **fields: Any) -> None:
    if not _DEBUG:
        return
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    try:
        path = home / "logs" / "risk-gate-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event, **fields}, default=str) + "\n")
    except OSError:
        pass


def _ollama_key() -> str:
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if key:
        return key
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("OLLAMA_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _history_text(history: Optional[list[Any]]) -> str:
    """Last few user/assistant messages, bounded, for resolving follow-ups."""
    parts: list[str] = []
    for item in (history or [])[-4:]:
        if isinstance(item, dict):
            role, content = item.get("role"), item.get("content")
        else:
            role, content = getattr(item, "role", None), getattr(item, "content", None)
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            parts.append(f"{role}: {content.strip()[:800]}")
    return "\n".join(parts)


def classify(text: str) -> str:
    """Return low/medium/high from the request text, with no network call."""
    text = text or ""
    if _HIGH_RE.search(text):
        return "high"
    if _MEDIUM_RE.search(text):
        return "medium"
    return "low"


def _clef_classify(text: str) -> Optional[str]:
    """Ask a typed decision model for the risk tier; None on any failure."""
    key = _ollama_key()
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
                    "security decisions even if they will not be sent or applied yet."
                ),
                "criteria": _CLEF_CRITERIA,
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
    except Exception as exc:
        logger.debug("risk-gated-verification: clef unavailable (%s)", exc)
        return None


def _policy_for(risk: str) -> Optional[str]:
    if risk == "low":
        return None
    if risk == "high":
        return (
            "[Verification policy: HIGH RISK]\n"
            "Before answering: (1) verify every externally checkable claim against a "
            "tool, source, file, test, or command; (2) do not guess names, dates, "
            "prices, versions, paths, permissions, or legal/policy details; (3) run "
            "the relevant test/lint/build or dry-run before claiming success; (4) "
            "distinguish verified facts from assumptions. If evidence is unavailable, "
            "say exactly what is unverified. For a client-facing or irreversible "
            "action, present the evidence and wait for the operator's approval "
            "instead of claiming completion."
        )
    return (
        "[Verification policy: MEDIUM RISK]\n"
        "This turn contains factual or technical claims. Check volatile facts "
        "(versions, prices, dates, current status) with a tool or authoritative "
        "source before stating them; inspect local files before claiming their "
        "contents. Mark anything you could not verify. Keep unsupported guesses "
        "out of the final answer."
    )


def _extract_json(text: str) -> Optional[dict[str, Any]]:
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


def _review_with_inkling(
    user_message: str,
    answer: str,
    risk: str,
    evidence: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Ask a different model for an adversarial review. None on any failure."""
    if _CTX is None:
        return None

    evidence_block = ""
    if evidence:
        evidence_block = (
            "\n\nTOOL EVIDENCE (authoritative, from this turn):\n"
            + "\n".join(evidence[-MAX_EVIDENCE:])[:6000]
        )

    prompt = (
        "You are an independent verifier. The user asked the task below, and another "
        "model produced the proposed answer. Your job is to find defects, not to "
        "praise.\n\n"
        "Rules:\n"
        "- Check every factual, technical, legal, financial, configuration, or "
        "client-facing claim.\n"
        "- Only report defects that affect correctness, safety, or client "
        "commitments. Ignore tone, style, emoji, and formatting unless they change "
        "meaning.\n"
        "- Treat the tool evidence as authoritative; do not flag a claim as "
        "unverifiable when the evidence supports it.\n"
        "- If you cannot verify a claim from the provided text, evidence, or general "
        "knowledge, say so.\n"
        "- Do not invent sources. Do not silently rewrite style.\n"
        "- Return JSON only with keys: risk (low/medium/high), verdict "
        "(pass/revise/block), issues (array of concrete strings), corrected_answer "
        "(string or null), evidence_needed (array of strings).\n"
        "- Use verdict=revise only when you provide a corrected answer; use block "
        "when the draft contains a critical unsupported claim and no safe "
        "correction is possible.\n\n"
        f"TASK:\n{user_message[:6000]}\n\n"
        f"PROPOSED ANSWER:\n{answer[:12000]}"
        f"{evidence_block}"
    )

    try:
        result = _CTX.llm.complete(
            [{"role": "user", "content": prompt}],
            model=VERIFIER_MODEL,
            temperature=0.1,
            max_tokens=VERIFIER_MAX_TOKENS,
            timeout=VERIFIER_TIMEOUT,
            purpose="risk-gated-verification",
        )
        parsed = _extract_json(getattr(result, "text", "") or "")
        if parsed is None:
            logger.debug(
                "risk-gated-verification: verifier returned non-JSON; leaving answer unchanged"
            )
            return None
        return _sanitize_review(answer, parsed)
    except Exception as exc:
        # Fail open; never hide the model's answer because a verifier broke.
        logger.info("risk-gated-verification: verifier unavailable (%s)", exc)
        return None


def _filter_quoted_issues(answer: str, issues: Any) -> list[str]:
    """Keep only issues whose quoted text literally appears in the answer."""
    if not isinstance(issues, list):
        return []
    normalized_answer = " ".join(str(answer).split()).lower()
    kept: list[str] = []
    for issue in issues:
        if not isinstance(issue, str):
            continue
        quotes = _QUOTE_RE.findall(issue)
        if any(
            " ".join(quote.split()).lower() in normalized_answer
            for quote in quotes
        ):
            kept.append(issue)
    return kept


def _sanitize_review(answer: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Drop issues that cannot quote the answer; if none survive, the review passes."""
    parsed["issues"] = _filter_quoted_issues(answer, parsed.get("issues"))
    if not parsed["issues"]:
        parsed["verdict"] = "pass"
        parsed["corrected_answer"] = None
        parsed["evidence_needed"] = []
    return parsed


def _format_issues(
    issues: list[Any],
    evidence: list[Any],
    source: str = "inkling",
) -> str:
    label = "Delegated verifier" if source == "delegated" else "Inkling review"
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


def _pre_llm_call(
    user_message: str = "",
    session_id: str = "",
    platform: str = "",
    conversation_history: Optional[list[Any]] = None,
    **_: Any,
) -> Optional[dict[str, str]]:
    history = _history_text(conversation_history)
    needs_context = bool(history) and (
        len((user_message or "").strip()) < 40
        or bool(_CONTEXT_RE.match(user_message or ""))
    )
    classify_input = (
        f"Recent conversation:\n{history}\nCurrent request: {user_message}"
        if needs_context
        else user_message
    )

    risk = classify(classify_input)

    # Fast path: obvious high-risk turns skip the classifier. For the ambiguous
    # middle, ask Clef (typed, ~0.5 s) and fall back to the regex verdict.
    use_clef = True
    try:
        use_clef = bool(_CTX.get_config("use_decision_model", True)) if _CTX is not None else True
    except Exception:
        pass

    if (
        use_clef
        and risk != "high"
        and (len((user_message or "").strip()) >= 24 or needs_context)
        and not _TRIVIAL_RE.match(user_message or "")
    ):
        clef_risk = _clef_classify(classify_input)
        if clef_risk:
            risk = clef_risk

    _debug(
        "pre_llm_call",
        session_id=session_id,
        risk=risk,
        chars=len(user_message or ""),
        contextual=needs_context,
    )

    with _LOCK:
        key = session_id or "default"
        _LAST_RISK[key] = {
            "risk": risk,
            "message": classify_input,
            "platform": platform,
        }
        _EVIDENCE[key] = []
        # Bound the map; a long-lived process should not grow forever.
        if len(_LAST_RISK) > 256:
            for old in list(_LAST_RISK)[:-128]:
                _LAST_RISK.pop(old, None)
                _EVIDENCE.pop(old, None)

    policy = _policy_for(risk)
    return {"context": policy} if policy else None


def _setting(name: str, default: Any) -> Any:
    """Read one plugin setting; config schema keys are top-level."""
    try:
        return _CTX.get_config(name, default) if _CTX is not None else default
    except Exception:
        return default


def _transform_llm_output(
    response_text: str = "",
    session_id: str = "",
    model: str = "",
    **_: Any,
) -> Optional[str]:
    if not response_text:
        return None

    with _LOCK:
        state = dict(_LAST_RISK.get(session_id or "default") or {})
    risk = str(state.get("risk") or "low")
    _debug(
        "transform",
        session_id=session_id,
        risk=risk,
        model=model,
        chars=len(response_text),
    )
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
        if isinstance(review.get("evidence_needed"), list)
        else []
    )
    corrected = review.get("corrected_answer")
    source = str(review.get("_source") or "inkling")

    if verdict == "pass":
        return None
    if verdict == "revise" and isinstance(corrected, str) and corrected.strip():
        if risk == "high":
            suffix = _format_issues(issues, needed, source) if (issues or needed) else ""
            return corrected.strip() + suffix
        # Medium risk: keep the draft and surface the review. A verifier rewrite
        # is not authoritative enough to replace a normal answer without operator sight.
        note = _format_issues(issues, needed, source)
        suggestion = f"\n\nSuggested correction: {corrected.strip()}"
        return response_text + (note or "") + suggestion
    if verdict == "block":
        prefix = "⚠️ **Verification blocked this answer.**\n"
        note = _format_issues(issues, needed, source)
        return prefix + response_text + ("\n" + note if note else "")
    # pass, or an unrecognized verdict: keep the draft. Append only if the
    # verifier found issues that survived quote checking.
    if issues or needed:
        return response_text + _format_issues(issues, needed, source)
    return None


def _post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    session_id: str = "",
    **_: Any,
) -> None:
    """Keep a short, authoritative evidence trail for the verifier."""
    if result is None:
        return
    if not isinstance(result, (str, bytes)):
        try:
            result = json.dumps(result, default=str)
        except (TypeError, ValueError):
            result = str(result)
    if isinstance(result, bytes):
        result = result.decode("utf-8", "replace")
    payload = (
        f"[{tool_name}]\n"
        f"args={json.dumps(args, default=str)[:800] if args is not None else ''}\n"
        f"result={str(result)[:2400]}"
    )
    with _LOCK:
        bucket = _EVIDENCE.setdefault(session_id or "default", [])
        bucket.append(payload)
        del bucket[:-MAX_EVIDENCE]


def _pre_verify(
    coding: bool = False,
    attempt: int = 0,
    changed_paths: Optional[list[str]] = None,
    **_: Any,
) -> Optional[dict[str, str]]:
    """One nudge per turn: after code edits, demand fresh evidence before finishing."""
    if not coding or attempt > 0:
        return None
    paths = [p for p in (changed_paths or []) if isinstance(p, str)]
    if not paths:
        return None
    return {
        "action": "continue",
        "message": (
            "Before finishing, run the relevant tests/linter/build for the changed "
            "code and report the exact command and result. If no test applies, state "
            "why and describe the manual check. Do not claim the change is verified "
            "without fresh evidence. Changed paths: " + ", ".join(paths[:12])
        ),
    }


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("pre_verify", _pre_verify)
    logger.info("risk-gated-verification registered")
