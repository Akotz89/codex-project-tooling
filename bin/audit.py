#!/usr/bin/env python3
"""Audit shipped consumer ownership. Use factory doctor for installed source readiness."""
import argparse
import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def worker_policy_conformance(home, source, bun):
    """Compare actual TypeScript behavior with the maintained Python owner cap."""
    source, bun = source.resolve(), bun.resolve()
    if not bun.is_file():
        raise ValueError('an exact existing Bun executable is required')
    expected = None
    tree = ast.parse((home / 'runtime/factory/jev_router.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'WORKER_EFFORTS' for target in node.targets):
            expected = ast.literal_eval(node.value)
    if not isinstance(expected, dict):
        raise ValueError('Python owner cap unavailable')
    policy = source / 'packages/provider-contract/src/worker-model-policy.ts'
    effort = source / 'packages/provider-contract/src/effort.ts'
    if policy.is_symlink() or effort.is_symlink() or not policy.is_file() or not effort.is_file():
        raise ValueError('actual provider-contract policy source required')
    models = sorted(expected) + ['__not_worker__']
    efforts = sorted(set().union(*expected.values()) |
                     {'minimal', 'ultra', 'persistent', '__not_effort__', ''})
    program = (
        'import {allowedWorkerPair} from ' + json.dumps(str(policy)) + ';'
        'import {EFFORT_LADDER} from ' + json.dumps(str(effort)) + ';'
        'const models=' + json.dumps(models) + ';'
        'const efforts=[...new Set([...EFFORT_LADDER,...' + json.dumps(efforts) + '])];'
        'process.stdout.write(JSON.stringify(models.flatMap(model=>efforts.map(effort=>'
        '({model,effort,allowed:allowedWorkerPair("codex",model,effort)})))));'
    )
    result = subprocess.run([str(bun), '--eval', program], cwd=source,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('provider cap evaluator failed; no conformance claim')
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or not rows:
        raise ValueError('provider cap evaluator returned no pairs')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'model', 'effort', 'allowed'}
                or not isinstance(row['allowed'], bool)
                or row['allowed'] != (row['effort'] in expected.get(row['model'], set()))):
            raise ValueError('Python/provider owner cap mismatch')
    return len(rows)

def audit(root, worker_policy_source=None, bun=None):
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
    if worker_policy_source is not None or bun is not None:
        try:
            if worker_policy_source is None or bun is None:
                raise ValueError("both --worker-policy-source and --bun are required")
            count = worker_policy_conformance(home, worker_policy_source, bun)
            print(f"WORKER_POLICY_CONFORMANCE pairs={count} matched")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            errors.append("Worker owner cap conformance failed")
    for error in errors:
        print(error)
    print(f"CONSUMER_AUDIT errors={len(errors)}; live source/provenance requires factory doctor")
    print(f"CONSUMER_AUDIT_CHECKS count={len(adapters.ENTRIES)}")
    return int(bool(errors))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent / "template")
    parser.add_argument("--worker-policy-source", type=Path,
                        help="Explicit owned Archon source for actual cross-language cap behavior")
    parser.add_argument("--bun", type=Path, help="Exact configured Bun executable")
    options = parser.parse_args()
    raise SystemExit(audit(options.repo, options.worker_policy_source, options.bun))
