"""Opaque custody references and the confirmed minimal lifecycle vocabulary."""

from dataclasses import dataclass
from enum import StrEnum


class LifecycleState(StrEnum):
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class CustodyAssignment:
    location_ref: str
    responsible_party_ref: str

    def __post_init__(self):
        for value in (self.location_ref, self.responsible_party_ref):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("custody references must be nonblank text without outer whitespace")
