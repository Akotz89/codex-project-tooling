"""Bound compatibility entry points for the single maintained factory runtime."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import types

ABI = 1
ENTRIES = (
    "factory/consumer.py", "factory/jev_prepared_bridge.py", "factory/jev_router.py",
    "factory/native_hook.py",
    "factory/runtime_host.py", "factory/runtime_process.py", "factory/runtime_resource.py",
    "factory/usage_collector.py", "harness/ci.py", "harness/appproc.py",
    "harness/mutations/run.py", "factory/tests/test_dispatch_shape.py",
    "factory/tests/test_usage_collector.py",
)
DEPENDENCIES = {
    "consumer": ("jev_router", "runtime_host", "jev_prepared_bridge", "native_hook"),
    "jev_prepared_bridge": ("jev_router", "consumer"),
    "native_hook": ("consumer", "jev_prepared_bridge", "jev_router"),
    "runtime_host": ("runtime_process", "runtime_resource"),
    "test_dispatch_shape": ("consumer", "jev_prepared_bridge", "jev_router"),
    "test_usage_collector": ("usage_collector",),
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


def source_files(source: Path) -> list[Path]:
    return [*sorted((source / "runtime").rglob("*.py")),
            source / "template/factory/pack.json",
            source / "template/factory/assessment_questions.json"]


def source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(source):
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("Shared factory source symlink refused")
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def binding(source: Path, project: dict | None) -> dict:
    return {"abi": ABI, "source": str(source), "revision": git(source, "rev-parse", "HEAD"),
            "digest": source_digest(source), "project": project}


def render(entry: str, bound: dict) -> bytes:
    if entry not in ENTRIES:
        raise ValueError("Unknown factory adapter entry")
    loader = Path(bound["source"]) / "runtime/adapter.py"
    loader_digest = hashlib.sha256(loader.read_bytes()).hexdigest()
    text = f'''"""Generated factory adapter, ABI {ABI}; reconfigure with the official installer."""
import hashlib as _hashlib
import importlib.util as _util
from pathlib import Path as _Path
import sys as _sys
_sys.dont_write_bytecode = True
_loader = _Path({str(loader)!r})
if not _loader.is_file() or _loader.is_symlink() or _hashlib.sha256(_loader.read_bytes()).hexdigest() != {loader_digest!r}:
    raise ValueError("Missing or drifted shared factory loader")
_name = "_factory_adapter_" + _hashlib.sha256(str(_loader.parent.parent).encode()).hexdigest()
if _name not in _sys.modules:
    _spec = _util.spec_from_file_location(_name, _loader)
    _module = _util.module_from_spec(_spec)
    _sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
_sys.modules[_name].installed(globals(), {entry!r}, {bound!r})
'''
    return text.encode()


def installed(namespace: dict, entry: str, bound: dict) -> None:
    if entry not in ENTRIES:
        raise ValueError("Unknown factory adapter entry")
    source = Path(bound["source"])
    if (bound["abi"] != ABI or source.is_symlink() or source.resolve() != source
            or git(source, "rev-parse", "HEAD") != bound["revision"]
            or source_digest(source) != bound["digest"]):
        raise ValueError("Shared factory source binding drift")
    adapter = Path(namespace["__file__"]).absolute()
    root = adapter.parents[len(Path(entry).parts) - 1]
    if adapter.is_symlink() or root / entry != adapter or not adapter.resolve().is_relative_to(root):
        raise ValueError("Factory adapter path refused")
    if git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ValueError("Factory adapter must belong to its project root")
    # The existing consumer owns the root/tracker/context contract. Loading it
    # here validates identity without requiring a provider or dispatching work.
    consumer = module(source, root, "factory/consumer.py")
    if not bound["project"] or consumer.project_binding(root) != bound["project"]:
        raise ValueError("Factory adapter project binding drift")
    if namespace["__name__"] == "__main__" and consumer.shared_root(Path.cwd()) != consumer.shared_root(root):
        raise ValueError("Factory adapter and caller must use the same project")
    settings = consumer.shared_root(root) / consumer.SETTINGS
    if settings.exists():
        consumer.read_settings(root)
    expose(namespace, source, root, entry)


def development(namespace: dict, entry: str, source: Path) -> None:
    sys.dont_write_bytecode = True
    if (source / "template" / entry).resolve() != Path(namespace["__file__"]).resolve():
        raise ValueError("Development adapter moved outside its source checkout")
    expose(namespace, source, source, entry, development=True)


def module(source: Path, root: Path, entry: str,
           *, development: bool = False):
    if entry not in ENTRIES:
        raise ValueError("Unknown factory adapter entry")
    package_name = package_key(source, root, development)
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = []
        sys.modules[package_name] = package
    name = Path(entry).stem
    canonical = package_name + "." + name
    if canonical in sys.modules:
        return sys.modules[canonical]
    target = types.ModuleType(canonical)
    target.__file__ = str(source / "template" / entry if development else root / entry)
    target.__package__ = package_name
    target.PROJECT_ROOT = (source / "template" if development and (
        entry.startswith("harness/") or entry == "factory/usage_collector.py") else root)
    target.SHARED_SOURCE = source
    sys.modules[canonical] = target
    setattr(sys.modules[package_name], name, target)
    try:
        for dependency in DEPENDENCIES.get(name, ()):
            module(source, root, "factory/" + dependency + ".py", development=development)
        if entry.startswith("factory/tests/"):
            publish(source, root, development)
        body = source / "runtime" / entry
        exec(compile(body.read_bytes(), str(body), "exec"), target.__dict__)
    except BaseException:
        sys.modules.pop(canonical, None)
        raise
    return target


def package_key(source: Path, root: Path, development: bool) -> str:
    identity = f"{source}\0{root}\0{development}"
    return "_factory_project_" + hashlib.sha256(identity.encode()).hexdigest()


def publish(source: Path, root: Path, development: bool) -> None:
    package_name = package_key(source, root, development)
    if "factory" not in sys.modules:
        factory = types.ModuleType("factory")
        factory.__path__ = [str(root / "factory")]
        sys.modules["factory"] = factory
    for entry in ENTRIES:
        if not entry.startswith("factory/") or "/tests/" in entry:
            continue
        name = Path(entry).stem
        target = sys.modules.get(package_name + "." + name)
        if target is not None:
            sys.modules[name] = target
            sys.modules["factory." + name] = target
            setattr(sys.modules["factory"], name, target)


def expose(namespace: dict, source: Path, root: Path, entry: str, *, development: bool = False) -> None:
    target = module(source, root, entry, development=development)
    publish(source, root, development)
    name = namespace["__name__"]
    if name == "__main__":
        if entry == "harness/appproc.py":
            return
        if entry.startswith("factory/tests/"):
            target.unittest.main(module=target)
        elif entry.endswith("/jev_router.py"):
            if sys.argv[1:] != ["--http-once"]:
                raise SystemExit(2)
            target._http_child()
        else:
            raise SystemExit(target.main())
    else:
        sys.modules[name] = target
