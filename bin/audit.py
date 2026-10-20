#!/usr/bin/env python3
"""Audit shipped consumer ownership. Use factory doctor for installed source readiness."""
import argparse
import ast
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True

def audit(root):
    errors = []
    home = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("audit_adapters", home / "runtime/adapter.py")
    adapters = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapters)
    implementation = home / "runtime"
    shared_names = {Path(entry).stem for entry in adapters.ENTRIES}
    for entry in adapters.ENTRIES:
        dependencies = set()
        tree = ast.parse((implementation / entry).read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    dependencies.update([node.module.split(".")[0]] if node.module
                                        else [alias.name for alias in node.names])
                elif node.module == "factory":
                    dependencies.update(alias.name for alias in node.names)
                elif node.module and node.module.startswith("factory."):
                    dependencies.add(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names if alias.name in shared_names)
        if dependencies != set(adapters.DEPENDENCIES.get(Path(entry).stem, ())):
            errors.append(f"Shared import dependencies drifted: {entry}")
    for entry in adapters.ENTRIES:
        path = root / entry
        try:
            tree = ast.parse(path.read_bytes())
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "installed"]
            if calls:
                bound = ast.literal_eval(calls[-1].args[-1])
                source = Path(bound["source"])
                if (source.resolve() != home or adapters.source_digest(source) != bound["digest"]
                        or adapters.git(source, "rev-parse", "HEAD") != bound["revision"]
                        or path.read_bytes() != adapters.render(entry, bound)):
                    errors.append(f"Drifted factory adapter: {entry}")
            elif path.read_bytes() != (home / "template" / entry).read_bytes():
                errors.append(f"Unknown factory adapter: {entry}")
        except (OSError, SyntaxError, ValueError, KeyError, IndexError) as error:
            errors.append(f"Invalid factory adapter {entry}: {error}")
    for path in [*root.rglob("*.py"), *implementation.rglob("*.py")]:
        if "__pycache__" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError) as error:
            errors.append(str(error))
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".yaml", ".yml"} or path.name == "SKILL.md":
            errors.append(f"Factory must not ship workflows or prompt skills: {path}")
    for name in ("harness/mutations/run.py",):
        text = (implementation / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] in {"subprocess", "consumer", "sdlc", "anthropic", "openai"} for m in modules):
                    errors.append(f"Execution dependency in data-only helper: {name}")
    for name in ("factory/runtime_host.py", "factory/runtime_process.py"):
        tree = ast.parse((implementation / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] in {"consumer", "sdlc", "anthropic", "openai"} for m in modules):
                    errors.append(f"Workflow/provider dependency in ordinary runtime host: {name}")
            if isinstance(node, ast.Call) and any(k.arg == "shell" and
                    not (isinstance(k.value, ast.Constant) and k.value.value is False)
                    for k in node.keywords):
                errors.append(f"Shell execution in ordinary runtime host: {name}")
    for error in errors:
        print(error)
    print(f"CONSUMER_AUDIT errors={len(errors)}; live source/provenance requires factory doctor")
    return int(bool(errors))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent / "template")
    raise SystemExit(audit(parser.parse_args().repo))
