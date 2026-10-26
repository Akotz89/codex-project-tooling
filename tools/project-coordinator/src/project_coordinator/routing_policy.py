"""Typed boundary implemented by project-root-owned routing providers."""
from typing import Any, Protocol, TypedDict


class SavedPolicy(TypedDict):
    model: str | None
    effort: str | None
    revision: str


class PolicySupport(TypedDict):
    state: str
    combined_supported: bool
    reason: str | None


class PolicyWrite(TypedDict, total=False):
    state: str
    saved: bool
    applied: bool
    revision: str
    error: str


class RoutingPolicyProvider(Protocol):
    def read_policy(self) -> SavedPolicy: ...
    def support(self) -> PolicySupport: ...
    def compare_and_swap(self, expected_revision: str, selection: dict[str, str]) -> PolicyWrite: ...


def valid_policy(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"model", "effort", "revision"}
            and (value["model"] is None or isinstance(value["model"], str))
            and (value["effort"] is None or isinstance(value["effort"], str))
            and isinstance(value["revision"], str))


def valid_support(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("combined_supported"), bool)
            and value.get("state") in {"supported", "unsupported"})
