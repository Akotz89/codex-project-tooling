"""Shared verification for durable caller promotion receipts.

The project caller supplies its pinned file reader and digest function.  This
module validates the generic terminal -> promotion manifest -> promoted-file
chain without interpreting project-specific source documents.
"""
from __future__ import annotations

import json
import builtins
import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
import re
from pathlib import Path
import stat
import sys
import types
from typing import Any, Callable


class Refused(Exception):
    """A pinned source could not be safely read or executed."""


class _ImportlibFacade(types.ModuleType):
    def __getattr__(self, name: str):
        if name != "util":
            raise Refused("unpinned_importlib_submodule")
        return super().__getattr__(name)


class _SysFacade(types.ModuleType):
    def __getattr__(self, name: str):
        if name == "modules":
            raise Refused("unpinned_sys_module_registry")
        raise Refused("unpinned_sys_attribute")


def read_pinned_source(path: Path, expected_sha256: str, *, limit: int = 2 * 1024 * 1024) -> bytes:
    """Read one stable regular-file image and require its digest before execution."""
    path = Path(path)
    if (type(expected_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or type(limit) is not int or limit <= 0):
        raise Refused("source_pin_parameters")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise Refused("source_file_type")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                             info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if len(raw) > limit or identity(before) != identity(after) or identity(before) != identity(current):
        raise Refused("source_file_changed")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise Refused("source_pin_before_execute")
    return raw


class _VerifiedBytesLoader:
    def __init__(self, path: Path, raw: bytes, injected_builtins: dict[str, Any]):
        self.path = Path(path)
        self.raw = raw
        self.injected_builtins = injected_builtins

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = str(self.path)
        module.__cached__ = None
        module.__dict__["__builtins__"] = self.injected_builtins
        exec(compile(self.raw, str(self.path), "exec"), module.__dict__)


def _verified_spec(name: str, path: Path, raw: bytes, injected_builtins: dict[str, Any]):
    loader = _VerifiedBytesLoader(path, raw, injected_builtins)
    spec = importlib.machinery.ModuleSpec(name, loader, origin=str(path))
    spec.has_location = True
    return spec


def _pinned_builtins(source_bytes: dict[Path, bytes] | None,
                     trusted_root: Path | None) -> dict[str, Any]:
    injected = dict(vars(builtins))
    if source_bytes is None and trusted_root is None:
        return injected
    if type(source_bytes) is not dict or trusted_root is None:
        raise Refused("verified_import_bindings")
    verified = {Path(path).resolve(strict=True): raw for path, raw in source_bytes.items()}
    if any(type(raw) is not bytes for raw in verified.values()):
        raise Refused("verified_import_bytes")
    root = Path(trusted_root).resolve(strict=True)
    util_facade = types.ModuleType("importlib.util")

    def pinned_spec(name, location, *args, **kwargs):
        path = Path(location).resolve(strict=True)
        raw = verified.get(path)
        if raw is not None:
            return _verified_spec(name, path, raw, injected)
        if path.is_relative_to(root):
            raise Refused("unpinned_executable_source")
        return importlib.util.spec_from_file_location(name, location, *args, **kwargs)

    util_facade.spec_from_file_location = pinned_spec
    util_facade.module_from_spec = importlib.util.module_from_spec
    importlib_facade = _ImportlibFacade("importlib")
    importlib_facade.util = util_facade
    sys_facade = _SysFacade("sys")
    for name in ("argv", "executable", "dont_write_bytecode"):
        setattr(sys_facade, name, getattr(sys, name))
    original_import = builtins.__import__

    def pinned_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name == "importlib":
            return importlib_facade
        if level == 0 and name == "importlib.util":
            return util_facade if fromlist else importlib_facade
        if level == 0 and name.startswith("importlib."):
            raise Refused("unpinned_importlib_submodule")
        if level == 0 and name == "sys":
            return sys_facade
        if level == 0 and name.startswith("sys."):
            raise Refused("unpinned_sys_attribute")
        return original_import(name, globals, locals, fromlist, level)

    injected["__import__"] = pinned_import
    return injected


def execute_verified_source(name: str, path: Path, raw: bytes, *,
                            source_bytes: dict[Path, bytes] | None = None,
                            trusted_root: Path | None = None):
    """Execute supplied bytes; nested pinned file loads use the same explicit map."""
    if type(raw) is not bytes:
        raise Refused("verified_source_bytes")
    path = Path(path)
    injected = _pinned_builtins(source_bytes, trusted_root)
    loader = _VerifiedBytesLoader(path, raw, injected)
    spec = importlib.machinery.ModuleSpec(name, loader, origin=str(path))
    spec.has_location = True
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__verified_source_sha256__ = hashlib.sha256(raw).hexdigest()
    module.__verified_source_bytes__ = raw
    return module


def load_pinned_module(name: str, path: Path, expected_sha256: str):
    raw = read_pinned_source(path, expected_sha256)
    return execute_verified_source(name, path, raw)




def consume_runtime_promotion(*, outcome_path: Path, promoted_path: Path,
                              expected_artifacts: Path, expected_relative: str,
                              private_directory: Callable[[Path], Path],
                              read_private: Callable[[Path], bytes],
                              digest: Callable[[bytes], str]) -> dict[str, Any]:
    """Verify a successful terminal outcome and the exact promoted-file row.

    Reads go through the caller's already-pinned private-file helper. The
    returned receipt is generic; the project caller remains responsible for
    validating the promoted document's schema, identity and source pins.
    """
    if not all(callable(value) for value in (private_directory, read_private, digest)):
        raise ValueError("receipt_helpers_required")
    outcome = json.loads(read_private(Path(outcome_path)))
    if (type(outcome) is not dict or outcome.get("state") != "complete"
            or outcome.get("scratch_cleanup") != "complete"
            or not _zero_retained_scratch(outcome.get("retained_scratch"))):
        raise ValueError("terminal_outcome_incomplete")

    artifact_summary = outcome.get("artifacts")
    manifest_ref = artifact_summary.get("manifest") if type(artifact_summary) is dict else None
    if type(manifest_ref) is not dict or set(manifest_ref) != {"path", "sha256"}:
        raise ValueError("promotion_manifest_reference")
    artifacts = Path(expected_artifacts)
    if not artifacts.is_absolute() or Path(private_directory(artifacts)) != artifacts:
        raise ValueError("promotion_artifacts_directory")
    manifest_path = Path(manifest_ref["path"])
    if manifest_path != artifacts / "manifest.json":
        raise ValueError("promotion_manifest_path")
    files = consume_promoted_files(
        manifest_reference=manifest_ref,
        expected_artifacts=artifacts,
        promoted_files={expected_relative: Path(promoted_path)},
        private_directory=private_directory, read_private=read_private, digest=digest)
    manifest_raw = files["manifest_bytes"]
    manifest_sha256 = files["manifest"]["sha256"]
    manifest = files["manifest_document"]
    promoted = Path(promoted_path)
    promoted_sha256 = files["files"][expected_relative]["sha256"]
    raw = files["files"][expected_relative]["bytes"]

    return {
        "complete": True,
        "files": manifest["files"],
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha256},
        "manifest_document": manifest,
        "promoted_path": str(promoted),
        "promoted_sha256": promoted_sha256,
        "promoted_bytes": raw,
        "terminal_state": outcome["state"],
        "scratch_cleanup": outcome["scratch_cleanup"],
    }


def consume_promoted_files(*, manifest_reference: dict[str, Any],
                           expected_artifacts: Path,
                           promoted_files: dict[str, Path],
                           private_directory: Callable[[Path], Path],
                           read_private: Callable[[Path], bytes],
                           digest: Callable[[bytes], str]) -> dict[str, Any]:
    """Verify and return exact bytes for a selected set of promoted files.

    This lower-level API is used by a project caller before its terminal
    outcome exists. The terminal API above reuses it after completion.
    """
    if not all(callable(value) for value in (private_directory, read_private, digest)):
        raise ValueError("receipt_helpers_required")
    artifacts = Path(expected_artifacts)
    if (not artifacts.is_absolute() or Path(private_directory(artifacts)) != artifacts
            or type(manifest_reference) is not dict
            or set(manifest_reference) != {"path", "sha256"}
            or type(promoted_files) is not dict or not promoted_files):
        raise ValueError("promotion_artifacts_directory")
    manifest_path = Path(manifest_reference["path"])
    if manifest_path != artifacts / "manifest.json":
        raise ValueError("promotion_manifest_path")
    manifest_raw = read_private(manifest_path)
    manifest_sha256 = digest(manifest_raw)
    if (not _is_digest(manifest_reference["sha256"])
            or manifest_sha256 != manifest_reference["sha256"]):
        raise ValueError("promotion_manifest_hash")
    manifest = json.loads(manifest_raw)
    if (type(manifest) is not dict or manifest.get("complete") is not True
            or type(manifest.get("files")) is not list):
        raise ValueError("promotion_manifest_incomplete")
    rows = manifest["files"]
    result = {}
    for expected_relative, promoted_path in promoted_files.items():
        relative = Path(expected_relative)
        promoted = Path(promoted_path)
        expected_path = artifacts / relative
        if (not expected_relative or relative.is_absolute()
                or relative.as_posix() != expected_relative
                or any(part in ("", ".", "..") for part in relative.parts)
                or promoted != expected_path or not promoted.is_absolute()):
            raise ValueError("promoted_file_path")
        raw = read_private(promoted)
        actual = digest(raw)
        matches = [row for row in rows if type(row) is dict
                   and row.get("path") == expected_relative]
        if (len(matches) != 1 or not _is_digest(matches[0].get("sha256"))
                or matches[0].get("sha256") != actual
                or type(matches[0].get("bytes")) is not int
                or matches[0]["bytes"] != len(raw)):
            raise ValueError("promoted_file_receipt")
        result[expected_relative] = {"bytes": raw, "sha256": actual}
    return {"complete": True, "manifest": {"path": str(manifest_path),
                                               "sha256": manifest_sha256},
            "manifest_document": manifest, "manifest_bytes": manifest_raw,
            "files": result}


def _is_digest(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _zero_retained_scratch(value: Any) -> bool:
    return (type(value) is dict
            and set(value) == {"logical_bytes", "allocated_bytes", "inodes"}
            and all(type(item) is int and item == 0 for item in value.values()))
