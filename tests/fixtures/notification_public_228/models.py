from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Dict, Optional


SCHEMA_VERSION = 1
MAX_PROMPT_CHARS = 8_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_instruction_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(
            "instruction id must be 1-128 characters and contain only "
            "letters, digits, '.', '_', ':', or '-'"
        )
    return value


def validate_prompt(value: str) -> str:
    if not value.strip():
        raise ValueError("instruction prompt must not be empty")
    if len(value) > MAX_PROMPT_CHARS:
        raise ValueError(f"instruction prompt exceeds {MAX_PROMPT_CHARS} characters")
    return value


@dataclass(frozen=True)
class Instruction:
    instruction_id: str
    source: str
    prompt: str
    prompt_sha256: str
    created_at: str
    state: str = "queued"
    target_session_id: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    updated_at: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        instruction_id: str,
        source: str,
        prompt: str,
        target_session_id: Optional[str] = None,
    ) -> "Instruction":
        instruction_id = validate_instruction_id(instruction_id)
        prompt = validate_prompt(prompt)
        if not source.strip():
            raise ValueError("instruction source must not be empty")
        if target_session_id is not None and (
            not target_session_id.strip() or len(target_session_id) > 256
        ):
            raise ValueError("target session id must be 1-256 characters")
        return cls(
            instruction_id=instruction_id,
            source=source,
            prompt=prompt,
            prompt_sha256=sha256_text(prompt),
            created_at=utc_now(),
            target_session_id=target_session_id,
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Instruction":
        instruction = cls(**value)
        validate_instruction_id(instruction.instruction_id)
        validate_prompt(instruction.prompt)
        if instruction.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported instruction schema {instruction.schema_version}"
            )
        if instruction.prompt_sha256 != sha256_text(instruction.prompt):
            raise ValueError("instruction prompt digest does not match content")
        if instruction.target_session_id is not None and (
            not instruction.target_session_id.strip()
            or len(instruction.target_session_id) > 256
        ):
            raise ValueError("invalid target session id")
        return instruction

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def with_state(self, state: str, session_id: str, turn_id: str) -> "Instruction":
        value = self.to_dict()
        value.update(
            state=state, session_id=session_id, turn_id=turn_id, updated_at=utc_now(),
        )
        return Instruction.from_dict(value)
