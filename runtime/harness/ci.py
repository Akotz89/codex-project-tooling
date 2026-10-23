#!/usr/bin/env python3
"""Ordinary static and unit checks. Runtime verification is a separate shared workflow."""
from __future__ import annotations
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
ROOT = PROJECT_ROOT
HERE = ROOT / "harness"
CONFIG = json.loads((HERE / "harness.config.json").read_text(encoding="utf-8"))

def unquote(token: str) -> str:
    """Take one layer of matching surrounding quotes off a token.

    Commands are split with `posix=False`, which is right on Windows because it
    leaves backslashes in paths alone -- and wrong in that it leaves the QUOTES
    attached to every token it split.

    That was applied to argv[0] only, and the consequence on the rest was severe:
    `python -c "import app"` arrived as the three tokens
    `python`, `-c`, `"import app"`, so Python evaluated the STRING LITERAL
    `"import app"` and exited 0. Measured: `python -c "import
    definitely_not_a_module"` also exits 0. The library driver's import check --
    the entire evidence behind `APP_STARTED driver=library` -- could not fail.
    The Node equivalent this shipped later had exactly the same hole.

    Stripping every token is what posix mode would have done, without giving up
    the backslashes.
    """
    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def resolve(argv: list[str]) -> list[str]:
    """Make argv[0] something the OS can actually execute.

    On Windows the tools people configure here -- npm, npx, yarn, pnpm, most JS
    tooling -- are `.cmd` shims, and subprocess without a shell does not consult
    PATHEXT. So a perfectly correct `"unit": "npm test"` fails with "the system
    cannot find the file specified", which reads like the tool is not installed when
    it is on PATH and works in any terminal.

    Legacy command strings are unquoted by run() after parsing. Structured argv
    is already literal, including any intentional surrounding quote characters.
    """
    if not argv:
        return argv
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def run(step: str, cmd: str | list[str], timeout: int = 600, *, cwd: Path = ROOT) -> tuple[int, str]:
    """One rung. A TIMEOUT IS A FAILURE, not a skip -- a hung check reports nothing."""
    argv = resolve([unquote(token) for token in shlex.split(cmd, posix=False)]
                   if isinstance(cmd, str) else list(cmd))
    try:
        p = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except OSError as e:
        return 127, f"could not run {argv[0] if argv else cmd!r}: {e}"


def fail(step: str, detail: str = "") -> int:
    """Name the rung that actually stopped the run.

    The gate asserts markers in a FIXED order rather than run order, so without this
    a suite that died early is reported as whichever marker is checked first -- true,
    and several rungs downstream of the cause. Misnaming your own failure is most of
    the cost of a failure nobody watched.
    """
    if detail:
        print(detail.strip()[-4000:], flush=True)
    print(f"GATE_FAILED: {step}", flush=True)
    return 1


def skipped(name: str, marker: str) -> None:
    """A rung with no command configured. LOUD, never silent.

    An unconfigured rung that printed nothing is indistinguishable from one that
    passed, which is the exact failure this whole file exists to prevent -- so
    absence is a fact in the log, and the gate can be told to require the marker.
    """
    print(f"{marker}_SKIPPED no '{name}' command in harness.config.json", flush=True)


def source_checks() -> list[tuple[str, list[str], re.Pattern]]:
    """Validate the complete declaration before running any configured command.

    Only argv and a positive discovered-result count are configurable. The
    adapter owns SHARED_SOURCE; callers cannot override cwd, source or env.
    Declaration order is execution order, and names identify failures uniquely.
    """
    if "source_checks" not in CONFIG:
        return []
    checks = CONFIG["source_checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("source_checks must be a nonempty list")
    validated = []
    names = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"name", "argv", "count_pattern"}:
            raise ValueError("each source check requires only name, argv and count_pattern")
        name, argv, pattern = check["name"], check["argv"], check["count_pattern"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                or name in names):
            raise ValueError("source check names must be unique lowercase identifiers")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)):
            raise ValueError(f"source check {name} requires nonempty argv strings")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"source check {name} requires a count_pattern")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"source check {name} has invalid count_pattern") from error
        if compiled.groups != 1:
            raise ValueError(f"source check {name} count_pattern requires one capture group")
        names.add(name)
        validated.append((name, argv, compiled))
    return validated


def main() -> int:
    if any(arg not in {"--quick"} for arg in sys.argv[1:]):
        print("Usage: python harness/ci.py [--quick] (static and unit checks only)")
        return 2
    print("HARNESS_START mode=ordinary", flush=True)
    try:
        checks = source_checks()
    except ValueError as error:
        return fail("configuration", f"SOURCE_CHECKS_ERROR: {error}")
    if not CONFIG.get("static") and not CONFIG.get("unit") and not checks:
        return fail("configuration", "NO_CHECKS: configure static, unit or source_checks commands")
    # --- 1. static -----------------------------------------------------------
    static_cmd = CONFIG.get("static") or ""
    if not static_cmd:
        skipped("static", "STATIC")
    else:
        rc, out = run("static", static_cmd)
        if rc != 0:
            return fail("static", out)
        print("STATIC_OK", flush=True)

    # --- 2. unit -------------------------------------------------------------
    unit_cmd = CONFIG.get("unit") or ""
    if not unit_cmd:
        skipped("unit", "UNIT")
    else:
        rc, out = run("unit", unit_cmd)
        if rc != 0:
            return fail("unit", out)
        pattern = (CONFIG.get("unit_count_pattern") or "").strip()
        if pattern:
            m = re.search(pattern, out)
            ran = int(m.group(1)) if m else 0
            # ZERO IS NOT A PASS. A suite that discovered nothing exits 0 and looks
            # perfect; both independent builds of this file added this guard
            # unprompted, which is how you know it is not paranoia.
            if ran == 0:
                return fail(
                    "unit",
                    "UNIT_ERROR: the runner reported 0 tests - a suite that ran nothing is "
                    "not a suite that passed. If the count is real, fix unit_count_pattern.\n"
                    + out[-2000:],
                )
            print(f"UNIT_PASSED tests={ran}", flush=True)
        else:
            print(
                "UNIT_PASSED tests=unknown (no unit_count_pattern set - a passing suite "
                "and an absent one look identical here)",
                flush=True,
            )

    # --- 3. explicitly registered checks of the adapter's bound source --------
    for name, argv, pattern in checks:
        step = f"source:{name}"
        rc, out = run(step, argv, cwd=SHARED_SOURCE)
        if rc != 0:
            return fail(step, out)
        matches = list(pattern.finditer(out))
        count = matches[0].group(1) if len(matches) == 1 else ""
        if not count or not re.fullmatch(r"[0-9]+", count) or int(count) == 0:
            return fail(step, "SOURCE_CHECK_ERROR: expected exactly one positive result count\n" + out[-2000:])
        skipped_count = re.search(r"^OK \([^\r\n)]*\bskipped=(\d+)[^\r\n)]*\)\s*$", out, re.MULTILINE)
        if skipped_count and int(skipped_count.group(1)) >= int(count):
            return fail(step, "SOURCE_CHECK_ERROR: all discovered tests were skipped\n" + out[-2000:])
        print(f"SOURCE_CHECK_PASSED name={name} count={int(count)} source={SHARED_SOURCE}", flush=True)

    print("RUNTIME_NOT_RUN: shared workflow verification is separate", flush=True)
    print("CHECKS_OK mode=ordinary", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
