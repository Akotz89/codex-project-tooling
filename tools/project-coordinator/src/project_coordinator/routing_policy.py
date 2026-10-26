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
    if (not isinstance(value, dict) or not isinstance(value.get("combined_supported"), bool)
            or value.get("state") not in {"supported", "unsupported"}
            or (value["state"] == "supported") != value["combined_supported"]):
        return False
    if "reason" in value and value["reason"] is not None and not isinstance(value["reason"], str):
        return False
    if "catalog_generated_at" in value and not isinstance(value["catalog_generated_at"], str):
        return False
    if "models" in value and (not isinstance(value["models"], dict) or any(
            not isinstance(model, str) or not isinstance(efforts, list)
            or any(not isinstance(effort, str) for effort in efforts)
            for model, efforts in value["models"].items())):
        return False
    return True


def valid_write(value: Any) -> bool:
    if (not isinstance(value, dict) or value.get("state") not in {"saved", "failed", "unsupported"}
            or not isinstance(value.get("saved"), bool) or not isinstance(value.get("applied"), bool)
            or not isinstance(value.get("revision"), str) or not value["revision"]
            or ("error" in value and not isinstance(value["error"], str))):
        return False
    return ((value["state"] == "saved") == value["saved"]
            and (value["saved"] or not value["applied"]))
