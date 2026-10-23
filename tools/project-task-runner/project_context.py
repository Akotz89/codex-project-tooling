"""Project-neutral, exact-byte context binding primitives for task callers."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def bind_project_context(runtime, project_root, canonical_state, project_id,
                         expected_code_root, legacy_project_binding=None):
    """Bind explicit project/state inputs before validating caller source maps."""
    root = Path(project_root)
    state = Path(canonical_state)
    code_root = Path(expected_code_root)
    if (not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir()
            or not state.is_absolute() or state.resolve(strict=True) != state
            or not state.is_relative_to(root)
            or not code_root.is_absolute() or code_root.resolve(strict=True) != code_root
            or type(project_id) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", project_id) is None):
        raise ValueError("project_context_binding")
    parameters = inspect.signature(runtime.bind_context).parameters
    if "project_id" in parameters:
        kwargs = {"project_id": project_id}
        if legacy_project_binding is not None:
            if "legacy_project_binding" not in parameters:
                raise ValueError("project_context_runtime_legacy_binding_api")
            kwargs["legacy_project_binding"] = legacy_project_binding
        bound = runtime.bind_context(root, state, **kwargs)
    else:
        # Older published runtime snapshots have a two-argument binder. Bind
        # the explicit identity on the loaded module immediately after the
        # root/state call so downstream checks retain the caller's project ID.
        bound = runtime.bind_context(root, state)
        runtime.PROJECT_ID = project_id
        if legacy_project_binding is not None:
            runtime.LEGACY_PROJECT_BINDING = legacy_project_binding
    if (bound != state or runtime.PROJECT_ROOT != root
            or runtime.CANONICAL_STATE != state or Path(runtime.CODE_ROOT) != code_root
            or getattr(runtime, "PROJECT_ID", None) != project_id):
        raise ValueError("project_context_runtime_binding")
    return state


def read_runtime_sources(expected_pins, code_root, caller_api):
    """Capture exact pinned Python source bytes beneath one explicit code root."""
    root = Path(code_root).resolve(strict=True)
    if type(expected_pins) is not dict or not expected_pins:
        raise ValueError("runtime_source_pin_shape")
    verified = {}
    for name, expected in expected_pins.items():
        path = Path(name)
        if path.suffix != ".py":
            continue
        lexical = Path(os.path.abspath(path))
        resolved = lexical.resolve(strict=True)
        if resolved != lexical or not lexical.is_relative_to(root):
            raise ValueError("runtime_source_path")
        verified[str(lexical)] = caller_api.read_pinned_source(resolved, expected)
    return verified


def execute_pinned_runtime(caller_api, module_name, runner_path, expected,
                           verified_sources, code_root):
    """Execute the same captured bytes that passed the supplied source pins."""
    path = Path(runner_path)
    raw = verified_sources.get(str(path))
    if raw is None or type(expected) is not str or digest(raw) != expected:
        raise ValueError("runtime_source_unpinned")
    return caller_api.execute_verified_source(
        module_name, path, raw,
        source_bytes={Path(name): value for name, value in verified_sources.items()},
        trusted_root=Path(code_root))


def read_selected_packet(selection, key, expected_prefix, project_root,
                         caller_api, *, limit=2 * 1024 * 1024):
    """Read one exact-pinned packet reference without path reopen races."""
    reference = selection.get(key) if type(selection) is dict else None
    if (type(reference) is not dict or set(reference) != {"path", "sha256"}
            or type(reference.get("path")) is not str
            or type(reference.get("sha256")) is not str):
        raise ValueError("packet_reference")
    path = Path(reference["path"])
    root = Path(project_root)
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not path.is_relative_to(root / "work")
            or not path.name.startswith(expected_prefix) or path.suffix != ".json"):
        raise ValueError("packet_path")
    raw = caller_api.read_pinned_source(path, reference["sha256"], limit=limit)
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("packet_shape")
    return value, {"path": str(path), "sha256": digest(raw)}


def read_pinned_packet_set(descriptors, project_root, caller_api, *, limit=2 * 1024 * 1024):
    """Capture, pin-check, and parse one bounded packet set from explicit data."""
    root = Path(project_root)
    if (not root.is_absolute() or root.resolve(strict=True) != root
            or type(descriptors) is not dict or not descriptors):
        raise ValueError("packet_set_binding")
    values, references = {}, {}
    for name, descriptor in descriptors.items():
        if (type(name) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) is None
                or type(descriptor) is not dict or set(descriptor) != {"path", "sha256"}
                or type(descriptor["path"]) is not str
                or type(descriptor["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]) is None):
            raise ValueError("packet_set_descriptor")
        relative = Path(descriptor["path"])
        path = root / relative
        if (relative.is_absolute() or any(part in (".", "..") for part in relative.parts)
                or relative.parts[:1] != ("work",)
                or path.resolve(strict=True) != path or not path.is_relative_to(root / "work")
                or path.suffix != ".json"):
            raise ValueError("packet_set_path")
        raw = caller_api.read_pinned_source(path, descriptor["sha256"], limit=limit)
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError("packet_set_shape")
        values[name] = value
        references[name] = {"path": str(path), "sha256": digest(raw)}
    return values, references


def validate_root_packet_chain(packets, references, contract, project_root):
    """Validate a packet chain against an independently pinned Root descriptor set."""
    if (type(packets) is not dict or type(references) is not dict
            or type(contract) is not dict or not isinstance(project_root, Path)):
        raise ValueError("packet_chain_shape")
    expected = contract.get("packets")
    if type(expected) is not dict or set(expected) != {"source_evidence", "candidate", "adoption"}:
        raise ValueError("packet_chain_contract")
    for key in expected:
        descriptor = expected[key]
        path = project_root / descriptor["path"]
        ref = {"path": str(path), "sha256": descriptor["sha256"]}
        if references.get(key) != ref:
            raise ValueError("packet_chain_root_pin")
    evidence = packets["source_evidence"]
    candidate = packets["candidate"]
    adoption = packets["adoption"]
    evidence_ref = references["source_evidence"]
    candidate_ref = references["candidate"]
    adoption_ref = references["adoption"]
    source = evidence.get("source")
    if (candidate.get("source_evidence") != evidence_ref
            or adoption.get("candidate") != candidate_ref["path"]
            or adoption.get("candidate_sha256") != candidate_ref["sha256"]
            or adoption.get("source_validation", {}).get("root_source_evidence") != evidence_ref
            or type(source) is not dict
            or source.get("observed_merge") != contract.get("observed_merge")
            or candidate.get("project_root") != str(project_root)
            or adoption.get("project_root") != str(project_root)
            or candidate.get("canonical_state") != contract.get("canonical_state")
            or adoption.get("canonical_state") != contract.get("canonical_state")
            or adoption.get("code_root") != contract.get("code_root")
            or adoption.get("runtime_path") != contract.get("runtime_path")
            or adoption.get("state") != contract.get("adoption_state")
            or adoption.get("actual_job_authorized") is not contract.get("actual_job_authorized")
            or adoption.get("actual_runtime_adoption_authorized") is not True):
        raise ValueError("packet_chain_binding")
    pins = candidate.get("runtime_source_pins")
    if (type(pins) is not dict or not pins
            or evidence.get("runtime_source_pins") != pins
            or adoption.get("runtime_source_pins") != pins):
        raise ValueError("packet_chain_runtime_pins")
    return {"source_evidence": evidence, "candidate": candidate, "adoption": adoption}


def bind_root_runtime_context(packets, references, contract, project_root, project_id,
                              caller_api, context_api):
    """Validate Root-owned packet data, bind project state, then execute pinned bytes."""
    root = Path(project_root)
    validated = validate_root_packet_chain(packets, references, contract, root)
    evidence, candidate, adoption = (validated["source_evidence"],
                                     validated["candidate"], validated["adoption"])
    state = Path(contract["canonical_state"])
    code_root = Path(contract["code_root"])
    runtime_path = Path(contract["runtime_path"])
    if (not state.is_absolute() or state.resolve(strict=True) != state or not state.is_relative_to(root)
            or not code_root.is_absolute() or code_root.resolve(strict=True) != code_root
            or not runtime_path.is_absolute() or runtime_path.resolve(strict=True) != runtime_path):
        raise ValueError("packet_chain_paths")
    pins = candidate["runtime_source_pins"]
    sources = read_runtime_sources(pins, code_root, caller_api)
    runner_path = runtime_path / "runner.py"
    runtime = execute_pinned_runtime(caller_api, contract.get("runtime_module_name", "adaptive_runtime"),
                                     runner_path, pins.get(str(runner_path)), sources, code_root)
    bound_state = bind_project_context(
        runtime, root, state, project_id, code_root,
        legacy_project_binding=contract.get("legacy_project_binding"))
    if (bound_state != state or runtime.PROJECT_ROOT != root or runtime.CANONICAL_STATE != state
            or Path(runtime.CODE_ROOT) != code_root or runtime.source_pins() != pins):
        raise ValueError("packet_chain_runtime_binding")
    for key in ("accounting_reader", "capacity_guard"):
        descriptor = candidate.get(key)
        if descriptor != adoption.get(key) or type(descriptor) is not dict \
                or set(descriptor) != {"path", "sha256"}:
            raise ValueError("packet_chain_accounting_binding")
        path = Path(descriptor["path"])
        if (not path.is_absolute() or path.resolve(strict=True) != path
                or path.parent != code_root / "accounting"):
            raise ValueError("packet_chain_accounting_path")
        caller_api.read_pinned_source(path, descriptor["sha256"], limit=4 * 1024 * 1024)
    return {"runtime": runtime, "adoption": adoption, "candidate": candidate,
            "source_evidence": evidence, "project_root": root, "canonical_state": state,
            "accounting_reader": candidate["accounting_reader"],
            "capacity_guard": candidate["capacity_guard"], "project_id": project_id,
            "root_packets": references}


def bind_selected_runtime_context(selection, historical, contract, project_root, project_id,
                                  caller_api, context_api):
    """Bind a selected packet set only when every reference is Root-pinned."""
    if (type(selection) is not dict
            or set(selection) != {"schema", "source_evidence", "candidate", "adoption"}
            or selection.get("schema") != contract.get("selection_schema")):
        raise ValueError("successor_context_schema")
    packets, refs = {}, {}
    prefixes = contract.get("selection_packet_prefixes")
    if (type(prefixes) is not dict or set(prefixes) != set(contract.get("packets", {}))
            or any(type(prefix) is not str or not prefix or "/" in prefix or "\\" in prefix
                   for prefix in prefixes.values())):
        raise ValueError("successor_context_prefix_contract")
    for key, prefix in prefixes.items():
        packets[key], refs[key] = read_selected_packet(
            selection, key, prefix, project_root, caller_api)
    selected = {"schema": selection["schema"], **refs}
    if selection != selected:
        raise ValueError("successor_context_selection_binding")
    # Root04 is a source-preparation packet with actual_job_authorized=false.
    # No selected packet can elevate that immutable grant; a future authorized
    # chain must be independently added to the trusted binding contract first.
    expected = contract["packets"]
    for key in expected:
        expected_ref = {"path": str(Path(project_root) / expected[key]["path"]),
                        "sha256": expected[key]["sha256"]}
        if refs.get(key) != expected_ref:
            raise ValueError("successor_context_not_root_pinned")
    bound = bind_root_runtime_context(packets, refs, contract, project_root, project_id,
                                      caller_api, context_api)
    bound["context_selection"] = selected
    bound["context_identity"] = digest(json.dumps(
        selected, sort_keys=True, separators=(",", ":")).encode())
    return bound
