from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from .models import EvaluateRequest, EvaluateResponse, Threat, Verdict


_PROMPT_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str, float]] = [
    (
        re.compile(r"\b(ignore|disregard)\b.*\b(previous|all)\b.*\b(instructions?|instruction)\b", re.I),
        "Instruction override pattern attempting to bypass system constraints.",
        0.75,
    ),
    (
        re.compile(r"\b(ignore|disregard)\b.*\b(system|developer)\b.*\bprompt\b", re.I),
        "Instruction override targeting system/developer guidance.",
        0.85,
    ),
    (
        re.compile(r"\b(system prompt|hidden prompt|developer message)\b", re.I),
        "Attempt to access hidden or privileged instructions.",
        0.80,
    ),
    (
        re.compile(r"\b(reveal|expose|print)\b.*\b(hidden|system|prompt)\b", re.I),
        "Request to disclose hidden prompt contents.",
        0.94,
    ),
    (
        re.compile(r"\b(jailbreak|jailbreaks)\b", re.I),
        "Jailbreak keyword pattern detected.",
        0.90,
    ),
    (
        re.compile(r"\b(act as|simulate)\b", re.I),
        "Role-play / impersonation pattern commonly used in jailbreaks.",
        0.60,
    ),
    (
        re.compile(r"\b(do anything now|unrestricted|no rules)\b", re.I),
        "Direct attempt to remove safety constraints.",
        0.88,
    ),
]


_JAILBREAK_PATTERNS: list[tuple[re.Pattern[str], str, float]] = [
    (
        re.compile(r"\b(override|bypass)\b.*\b(safety|guardrail|policy|rules?)\b", re.I),
        "Bypass attempt against safety policies/guardrails.",
        0.80,
    ),
    (
        re.compile(r"\b(disregard)\b.*\b(previous|above)\b.*\b(instructions?)\b", re.I),
        "Disregard instruction sequence / precedence attack.",
        0.75,
    ),
    (
        re.compile(r"\b(complete|fully)\b.*\bwithout\b.*\blimitations?\b", re.I),
        "Removal of limitations pattern detected.",
        0.65,
    ),
]


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?\d{1,2}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
)
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_BLOCK_USER_MESSAGE = (
    "This response was blocked by the safety guardrail. "
    "The original model output is not shown to protect users."
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _count_regex_matches(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text))


def _redact_by_regex(text: str, pattern: re.Pattern[str], replacement: str) -> tuple[str, int]:
    matches = list(pattern.finditer(text))
    if not matches:
        return text, 0

    # Replace from the end so offsets stay valid.
    redacted = text
    for m in reversed(matches):
        redacted = redacted[: m.start()] + replacement + redacted[m.end() :]
    return redacted, len(matches)


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """
    Redacts common PII patterns using regex heuristics.
    Returns: (redacted_text, counts_by_type)
    """
    counts: dict[str, int] = {}
    redacted = text

    for type_name, regex, repl in [
        ("EMAIL", _EMAIL_RE, "[REDACTED_EMAIL]"),
        ("SSN", _SSN_RE, "[REDACTED_SSN]"),
        ("PHONE", _PHONE_RE, "[REDACTED_PHONE]"),
        ("PAYMENT", _CREDIT_CARD_RE, "[REDACTED_PAYMENT]"),
    ]:
        redacted, c = _redact_by_regex(redacted, regex, repl)
        if c:
            counts[type_name] = c

    return redacted, counts


def _prompt_injection_detector(prompt: str) -> Optional[Threat]:
    p = prompt or ""
    score = 0.0
    reasons: list[str] = []
    for regex, explanation, weight in _PROMPT_INJECTION_PATTERNS:
        if regex.search(p):
            score = max(score, weight)
            reasons.append(explanation)
    if not reasons:
        return None
    return Threat(category="Prompt Injection / Hidden Instructions", score=_clamp01(score), explanation=" ".join(reasons))


def _jailbreak_detector(prompt: str) -> Optional[Threat]:
    p = prompt or ""
    score = 0.0
    reasons: list[str] = []
    for regex, explanation, weight in _JAILBREAK_PATTERNS:
        if regex.search(p):
            score = max(score, weight)
            reasons.append(explanation)
    if not reasons:
        return None
    return Threat(category="Jailbreak Pattern", score=_clamp01(score), explanation=" ".join(reasons))


def _pii_detector_and_redact(text: str) -> tuple[Optional[Threat], Optional[str]]:
    redacted, counts = redact_pii(text or "")
    if not counts:
        return None, None

    score = 0.0
    if "SSN" in counts:
        score += 0.65
    if "EMAIL" in counts:
        score += 0.35
    if "PHONE" in counts:
        score += 0.25
    if "PAYMENT" in counts:
        score += 0.45

    # Normalize by rough magnitude (keep SSN high).
    score = _clamp01(score / 1.0)

    details = ", ".join([f"{k}x{v}" for k, v in counts.items()])
    threat = Threat(
        category="Potential PII Exposure",
        score=score,
        explanation=f"Detected PII-like patterns ({details}). Sensitive values should be redacted before showing to end users.",
    )
    return threat, redacted


def _hallucination_risk_detector(response: str, prompt: str) -> Optional[Threat]:
    """
    Heuristic-only hallucination risk estimation.
    (We don't have ground truth, so this flags responses likely to contain unsupported facts.)
    """
    r = (response or "").strip()
    if not r:
        return None

    confident_markers = [
        "definitely",
        "certainly",
        "guarantee",
        "always",
        "never",
        "exactly",
        "without a doubt",
    ]
    hedge_markers = ["might", "could", "may", "possibly", "probably", "i think", "as far as", "likely"]

    confident_hits = sum(1 for m in confident_markers if re.search(r"\b" + re.escape(m) + r"\b", r, re.I))
    hedge_hits = sum(1 for m in hedge_markers if re.search(r"\b" + re.escape(m) + r"\b", r, re.I))

    # Numeric + year density is often correlated with factual claims.
    numeric_hits = len(re.findall(r"\b\d{2,}\b", r))
    year_hits = len(re.findall(r"\b(19|20)\d{2}\b", r))

    # Count assertion-like sentences.
    sentences = re.split(r"[.!?]\s+", r)
    assertion_like = 0
    for s in sentences:
        if re.search(r"\b(is|are|was|were|means|results in|leads to|causes)\b", s, re.I):
            assertion_like += 1

    # Soft scoring curve.
    # Higher confident/number/assertion -> higher risk; hedging reduces it.
    raw = 0.08
    raw += 0.03 * min(numeric_hits, 15)
    raw += 0.05 * min(year_hits, 8)
    raw += 0.04 * min(assertion_like, 12)
    raw += 0.12 * min(confident_hits, 6)
    raw -= 0.06 * min(hedge_hits, 10)

    # Damp extreme cases slightly.
    raw = 1.0 - math.exp(-raw)
    score = _clamp01(raw)

    if score < 0.55:
        return None

    # Explanation that points to heuristic signals.
    explanation = (
        f"Response contains potentially factual assertions with signals like {numeric_hits} numeric tokens and "
        f"{year_hits} years, plus {confident_hits} high-confidence marker(s)."
    )
    if hedge_hits:
        explanation += f" Hedging signals were also present ({hedge_hits}), which can reduce—but not eliminate—risk."

    # Optionally reference whether prompt asked for something verifiable.
    if re.search(r"\b(what|who|when|where|how many)\b", prompt or "", re.I):
        explanation += " The prompt suggests fact-oriented answering."

    return Threat(category="Hallucination Risk (Heuristic)", score=score, explanation=explanation)


def _block_message(threats: list[Threat]) -> str:
    if not threats:
        return _BLOCK_USER_MESSAGE

    primary = max(threats, key=lambda t: t.score)
    return f"{_BLOCK_USER_MESSAGE} Detected risk: {primary.category}. {primary.explanation}"


def _generate_model_response(prompt: str) -> str:
    import os
    import json
    import urllib.request

    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "openrouter/free")
    
    if not api_key:
        return "Error: OPENROUTER_API_KEY is not set in the environment."
        
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "http://127.0.0.1:8000",
        "X-Title": "Aiden Guardrail System"
    }
    
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }
    
    req = urllib.request.Request(
        url, 
        data=json.dumps(body).encode("utf-8"), 
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as res:
            res_body = json.loads(res.read().decode("utf-8"))
            return res_body["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error calling OpenRouter: {str(e)}"


def evaluate_guardrails(req: EvaluateRequest) -> EvaluateResponse:
    import os
    prompt = req.prompt or ""
    model_response = req.model_response

    if not model_response and os.getenv("OPENROUTER_API_KEY"):
        model_response = _generate_model_response(prompt)

    threats: list[Threat] = []
    redacted_response: Optional[str] = None
    safe_response: Optional[str] = None

    injection = _prompt_injection_detector(prompt)
    if injection:
        threats.append(injection)

    jailbreak = _jailbreak_detector(prompt)
    if jailbreak:
        threats.append(jailbreak)

    pii_threat: Optional[Threat] = None
    if model_response is not None:
        pii_threat, redacted = _pii_detector_and_redact(model_response)
        if pii_threat and redacted is not None:
            redacted_response = redacted
            threats.append(pii_threat)
    else:
        # Still check prompt for PII if response is missing.
        pii_threat, redacted = _pii_detector_and_redact(prompt)
        if pii_threat and redacted is not None:
            redacted_response = redacted
            threats.append(pii_threat)

    hallucination = _hallucination_risk_detector(model_response, prompt)
    if hallucination:
        threats.append(hallucination)

    # Overall score: max threat score.
    overall = max((t.score for t in threats), default=0.0)

    hallucination_score = hallucination.score if hallucination else 0.0

    verdict: Verdict
    action_message: str

    # Block on any prompt-side attack before content reaches the user.
    if injection is not None or jailbreak is not None:
        verdict = "block"
        safe_response = _block_message(threats)
        action_message = "Blocked: prompt injection or jailbreak detected. Model output withheld."
    elif hallucination_score >= 0.7:
        verdict = "block"
        safe_response = _block_message(threats)
        action_message = "Blocked: high hallucination risk. Unverified factual claims withheld."
    elif len([t for t in threats if t.category != "Potential PII Exposure"]) > 0:
        verdict = "block"
        safe_response = _block_message(threats)
        action_message = "Blocked: safety threats detected. Model output withheld."
    elif pii_threat is not None:
        verdict = "allow_with_redaction"
        safe_response = redacted_response
        action_message = "PII detected and redacted before delivery to the user."
    else:
        verdict = "allow"
        safe_response = model_response
        action_message = "No critical issues detected. Safe to show to the user."

    return EvaluateResponse(
        verdict=verdict,
        overall_safety_score=_clamp01(overall),
        threats=threats,
        safe_response=safe_response,
        action_message=action_message,
    )

