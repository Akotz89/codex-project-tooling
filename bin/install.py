"""Install the factory into an application and preserve the user's own files."""
from __future__ import annotations
import hashlib
import json
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
TEMPLATE = HOME / "template"
sys.path.insert(0, str(TEMPLATE / "factory"))
import consumer

_adapter_spec = importlib.util.spec_from_file_location(
    "factory_install_adapters", HOME / "runtime/adapter.py")
adapters = importlib.util.module_from_spec(_adapter_spec)
_adapter_spec.loader.exec_module(adapters)

# Files the user writes for their project. Installed once, never overwritten.
PERSONAL = {"harness/harness.config.json", "harness/runtime.inputs.json",
            "harness/END-TO-END.md", "harness/mutations/defects.json", "MISSION.md",
            "FACTORY.md", "FACTORY_RULES.md", ".factory/holdout/HOLDOUT.md"}
# Shared workflows read this root conventions file. Point to the canonical policy
# in their already-bound source checkout rather than installing another copy.
POLICY_POINTER = (b"Factory workflow policy: read `template/factory/WORKFLOW_POLICY.md` "
                  b"in the shared source checkout bound by the generated `factory/consumer.py` adapter.\n")
OLD_POLICY_POINTER = (b"See [factory workflow policy](factory/WORKFLOW_POLICY.md) for "
                      b"factory-specific shared-workflow requirements.")
LOCAL_POLICY_POINTER = OLD_POLICY_POINTER + b"\n"
STATE_HOME_ENV = "CODEX_PROJECT_TOOLING_STATE_HOME"


def within(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Install path resolves outside application: {path}")
    return path


def shared_state_root() -> Path:
    override = os.environ.get(STATE_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return (base / "codex-project-tooling").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/codex-project-tooling").resolve()
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return (base / "codex-project-tooling").resolve()


def backup_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    source = within(root, source)
    state = shared_state_root()
    if state.is_relative_to(root):
        raise ValueError(f"Shared state must be outside the project repository: {state}")
    project_key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    destination = state / "backups" / project_key / source.relative_to(root)
    resolved_destination = destination.resolve()
    if not resolved_destination.is_relative_to(state):
        raise ValueError(f"Backup path resolves outside shared state: {destination}")
    if resolved_destination.is_relative_to(root):
        raise ValueError(f"Backup path resolves inside the project repository: {destination}")
    return destination


def backup(root: Path, path: Path, dry: bool) -> None:
    source = within(root, path)
    dest = backup_path(root, source)
    print(f"preserve {path.relative_to(root)} -> {dest}")
    if not dry:
        relative_path = source.relative_to(root)
        project_backup_root = dest.parents[len(relative_path.parts) - 1]
        project_backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            project_backup_root.chmod(0o700)
            dest.parent.chmod(0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp",
                                               dir=dest.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(path, temporary)
            os.replace(temporary, dest)
        finally:
            temporary.unlink(missing_ok=True)


def install_policy_pointer(root: Path, dry: bool) -> None:
    path = within(root, root / "engineering.md")
    local_policy = root / "factory/WORKFLOW_POLICY.md"
    pointer = POLICY_POINTER
    if (local_policy.is_symlink()
            or (local_policy.is_file() and local_policy.read_bytes()
                != (TEMPLATE / "factory/WORKFLOW_POLICY.md").read_bytes())):
        pointer = LOCAL_POLICY_POINTER
    previous = path.read_bytes() if path.exists() else b""
    lines = previous.splitlines(keepends=True)
    if any(line.rstrip(b"\r\n") == pointer.rstrip(b"\r\n") for line in lines):
        return
    print("install engineering.md factory policy pointer")
    if not dry:
        obsolete_pointer = (
            POLICY_POINTER.rstrip(b"\r\n") if pointer == LOCAL_POLICY_POINTER
            else OLD_POLICY_POINTER.rstrip(b"\r\n"))
        if any(line.rstrip(b"\r\n") == obsolete_pointer for line in lines):
            updated = b"".join(
                (pointer.rstrip(b"\n") + (b"\r\n" if line.endswith(b"\r\n") else b"\n"))
                if line.rstrip(b"\r\n") == obsolete_pointer else line
                for line in lines
            )
        else:
            separator = b"" if not previous or previous.endswith((b"\n", b"\r")) else b"\n"
            gap = b"" if not previous or previous.endswith((b"\n\n", b"\r\n\r\n")) else b"\n"
            updated = previous + separator + gap + pointer
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                               dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                shutil.copystat(path, temporary)
            os.replace(temporary, path)
        finally:
            if fd != -1:
                os.close(fd)
            temporary.unlink(missing_ok=True)


def sync(root: Path, dry: bool = False) -> None:
    root = root.resolve()
    try:
        project = consumer.project_binding(root)
    except (OSError, ValueError, KeyError):
        project = None
    bound = adapters.binding(HOME, project)
    policy_path = within(root, root / "factory/WORKFLOW_POLICY.md")
    policy_source = TEMPLATE / "factory/WORKFLOW_POLICY.md"
    if (policy_path.is_file() and not policy_path.is_symlink()
            and policy_path.read_bytes() == policy_source.read_bytes()):
        backup(root, policy_path, dry)
        print("remove redundant project copy of shared workflow policy")
        if not dry:
            policy_path.unlink()
    # Changed managed files are backed up before replacement; personal files are
    # never overwritten. The generic policy copy is removed separately above.
    for src in sorted(TEMPLATE.rglob("*")):
        # Development adapters keep disposable runtime/test data under the
        # template's work/. It is never a distributable project scaffold.
        if src.relative_to(TEMPLATE).parts[0] == "work":
            continue
        if not src.is_file() or "__pycache__" in src.parts or src.suffix in {".pyc", ".pyo"}:
            continue
        rel = src.relative_to(TEMPLATE).as_posix()
        if rel in {"gitignore-additions.txt", "factory/WORKFLOW_POLICY.md"}:
            continue
        dest = within(root, root / rel)
        content = adapters.render(rel, bound) if rel in adapters.ENTRIES else src.read_bytes()
        if dest.exists():
            if rel in PERSONAL or dest.read_bytes() == content:
                continue
            backup(root, dest, dry)
        print(f"install {rel}")
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            shutil.copystat(src, dest)
    install_policy_pointer(root, dry)
    ignore = within(root, root / ".gitignore")
    previous = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    additions = [line for line in (TEMPLATE / "gitignore-additions.txt").read_text().splitlines()
                 if line and not line.startswith("#") and line not in previous.splitlines()]
    if additions and not dry:
        with ignore.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(additions) + "\n")
    print("Project files are preserved; rollback copies are kept in shared state outside the repository.")


def install_source(repository: str, revision: str, cache: Path, bun: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("--revision must be a full lowercase commit SHA")
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    dest = within(cache, cache / revision)
    settings = {"source": str(dest), "revision": revision, "bun": bun,
                "repository": repository, "kind": "integration"}
    if not dest.exists():
        # An independent clone, never a checkout switch in an operator's source.
        consumer.checked(["git", "clone", "--no-hardlinks", "--no-checkout", repository, str(dest)], cache, 900)
        consumer.checked(["git", "checkout", "--detach", revision], dest)
    actual = consumer.checked(["git", "rev-parse", "HEAD"], dest).strip()
    if actual != revision:
        raise ValueError(f"Existing cache has wrong SHA ({actual}); it will not be repointed")
    dirty = consumer.checked(["git", "status", "--porcelain", "--untracked-files=all"], dest)
    if dirty.strip():
        raise ValueError("Existing cache is dirty; it will not be overwritten")
    if not (dest / "node_modules").is_dir():
        consumer.checked([bun, "install", "--frozen-lockfile"], dest, 1800)
    consumer.doctor(settings)
    return settings


def validate_router_options(settings: dict) -> None:
    if "jev_router" not in settings:
        return
    router = settings["jev_router"]
    if not isinstance(router, dict) or router.keys() - {"key_file", "jev_model", "allowed_models", "allowed_efforts"}:
        raise ValueError("Invalid jev_router settings: unsupported options")
    from jev_router import validate_allowed_efforts, validate_allowed_models
    if "allowed_models" in router:
        validate_allowed_models(router["allowed_models"])
    if "allowed_efforts" in router:
        validate_allowed_efforts(router["allowed_efforts"])
    key_file = router.get("key_file")
    if key_file is not None and (not isinstance(key_file, str) or "\0" in key_file):
        raise ValueError("Invalid jev_router.key_file: expected a file reference")
    if "jev_model" in router and (not isinstance(router["jev_model"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", router["jev_model"])):
        raise ValueError("Invalid jev_router.jev_model: expected a supported model identifier")


def validate_project_settings(root: Path, settings: dict | None = None, *, check_binding: bool = True) -> dict | None:
    """Validate current persisted state before init can cause installer side effects."""
    root = root.resolve()
    path = within(consumer.shared_root(root), consumer.shared_root(root) / consumer.SETTINGS)
    if settings is None:
        if not path.exists():
            return None
        settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"Invalid consumer settings: {path}")
    if check_binding and consumer.MANIFEST.get("project_binding_required"):
        binding = consumer.project_binding(root)
        if settings.get("project_binding") != binding:
            raise ValueError("Project binding drift: root, local tracker address or context changed; explicitly reconfigure this project")
    validate_router_options(settings)
    if settings.get("saved_routing_policy") is not None:
        from jev_router import (validate_allowed_efforts, validate_allowed_models,
                                validate_saved_routing_policy)
        saved = validate_saved_routing_policy(settings["saved_routing_policy"])
        router = settings.get("jev_router", {})
        models = validate_allowed_models(router["allowed_models"]) if "allowed_models" in router else None
        efforts = validate_allowed_efforts(router["allowed_efforts"]) if "allowed_efforts" in router else None
        if ((models is not None and saved["model"] not in models)
                or (efforts is not None and saved["effort"] not in efforts.get(saved["model"], []))):
            raise ValueError("saved routing pair is outside project ceilings")
    return settings


def configure(root: Path, settings: dict) -> None:
    settings = dict(settings)
    if consumer.MANIFEST.get("project_binding_required"):
        settings["project_binding"] = consumer.project_binding(root)
    path = within(consumer.shared_root(root), consumer.shared_root(root) / consumer.SETTINGS)
    validate_project_settings(root, settings)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        validate_project_settings(root, previous, check_binding=False)
        binding = settings.get("project_binding")
        prior_binding = previous.get("project_binding")
        if (isinstance(binding, dict) and isinstance(prior_binding, dict)
                and all(prior_binding.get(key) == binding[key]
                        for key in ("root", "tracker", "project"))
                ):
            for key in ("jev_router", "operator_metadata"):
                if key not in settings and key in previous:
                    settings[key] = previous[key]
            if settings.get("saved_routing_policy") is None and previous.get("saved_routing_policy") is not None:
                settings["saved_routing_policy"] = previous["saved_routing_policy"]
    validate_project_settings(root, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
