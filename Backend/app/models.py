from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Threat(BaseModel):
    category: str = Field(..., description="High-level threat category")
    score: float = Field(..., ge=0.0, le=1.0, description="Safety score for this threat")
    explanation: str = Field(..., description="Human-readable explanation")


Verdict = Literal["allow", "allow_with_redaction", "block"]


class EvaluateRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    prompt: str = Field(..., description="User prompt")
    model_response: Optional[str] = Field(None, description="Model response to be checked")


class EvaluateResponse(BaseModel):
    verdict: Verdict
    overall_safety_score: float = Field(..., ge=0.0, le=1.0)
    threats: list[Threat]

    # Only content safe to show the end user. Original model output is never leaked on block.
    safe_response: Optional[str] = None

    # Short action summary for UI.
    action_message: str
