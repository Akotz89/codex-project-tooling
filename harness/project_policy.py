"""Caller-owned identities for the four retained-evidence workflow purposes."""
import json
from pathlib import Path
import re


PURPOSES = ("implementation", "review", "runtime_suite", "wrong_identity")


def canonical(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and "\0" not in value


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {
            "project_id", "worker_roles", "linear_workspace", "acceptance_issue", "workflows"}:
        raise ValueError("explicit project policy with exactly the documented fields is required")
    if not canonical(policy["project_id"]):
        raise ValueError("policy: invalid project_id")
    roles = policy["worker_roles"]
    if (not isinstance(roles, list) or not roles
            or not all(canonical(role) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role) for role in roles)
            or len(set(roles)) != len(roles)):
        raise ValueError("policy: worker_roles must be nonempty unique canonical strings")
    if not canonical(policy["linear_workspace"]) or not re.fullmatch(r"[A-Za-z0-9_-]+", policy["linear_workspace"]):
        raise ValueError("policy: invalid Linear workspace")
    if not canonical(policy["acceptance_issue"]) or not re.fullmatch(r"[A-Z][A-Z0-9]*-[0-9]+", policy["acceptance_issue"]):
        raise ValueError("policy: invalid acceptance issue")
    workflows = policy["workflows"]
    if not isinstance(workflows, dict) or set(workflows) != set(PURPOSES):
        raise ValueError("policy: exactly four semantic workflow purposes are required")
    for entry in workflows.values():
        if (not isinstance(entry, dict) or set(entry) != {"workflow", "node"}
                or not all(canonical(value) for value in entry.values())):
            raise ValueError("policy: each purpose requires canonical workflow and node strings")
    if len({entry["workflow"] for entry in workflows.values()}) != len(PURPOSES):
        raise ValueError("policy: workflow purposes must identify distinct workflows")
    return policy


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def load_policy(path):
    with Path(path).open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("policy exceeds 1 MiB")
    return validate_policy(json.loads(data, object_pairs_hook=unique_object,
                                      parse_constant=invalid_constant))
