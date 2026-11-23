"""
app/agent/identity/lookup.py

Extract identity information from one caller turn.
Used by the inbound identity nodes.
"""

import logging
from datetime import date
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage # type: ignore
from pydantic import BaseModel, Field, field_validator

from app.agent.llm import get_llm

logger = logging.getLogger(__name__)

IdentityStage = Literal[
    "confirm_name",
    "verify_name",
    "verify_dob",
    "new_name",
    "new_dob",
]


class IdentityTurn(BaseModel):
    """Only information clearly stated by the caller in this turn."""

    full_name: str | None = Field(
        default=None,
        description="Full name spoken by the caller, transliterated into Latin letters.",
    )
    date_of_birth: str | None = Field(
        default=None,
        description="Clearly stated date of birth in YYYY-MM-DD format.",
    )
    confirmation: Literal["yes", "no", "unclear"] = Field(
        default="unclear",
        description="Answer to the name-confirmation question, if explicitly given.",
    )
    refused: bool = Field(
        default=False,
        description="True only if the caller explicitly refuses identity verification.",
    )
    third_party: bool = Field(
        default=False,
        description="True only if the caller explicitly says they are someone else.",
    )

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if not value:
            return None
        return " ".join(value.split()) or None

    @field_validator("date_of_birth")
    @classmethod
    def validate_dob(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return parsed.isoformat() if parsed <= date.today() else None


def extract_identity_turn(text: str, stage: IdentityStage) -> IdentityTurn | None:
    """Extract facts from one turn. Return None if extraction fails."""
    try:
        return get_llm().with_structured_output(IdentityTurn).invoke([
            SystemMessage(
                "Extract only what the caller explicitly says. "
                "The caller may speak English, Hindi or Odia. "
                "Transliterate spoken names into Latin letters. "
                "Convert a complete, clearly stated date of birth to YYYY-MM-DD. "
                "Never guess missing details or copy a name from a question. "
                "Set confirmation to yes or no only when the caller explicitly "
                "answers the name-confirmation question. Otherwise use unclear. "
                "Set refused only for an explicit refusal to verify identity. "
                "Set third_party only if the caller says they are someone else. "
                f"The current identity stage is: {stage}."
            ),
            HumanMessage(text),
        ])
    except Exception:
        logger.exception("Identity extraction failed")
        return None