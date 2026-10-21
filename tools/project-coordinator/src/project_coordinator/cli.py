"""Small JSON command interface; native requests are executed by the assistant."""
import argparse
import json
import sys

from .runtime import InvalidEvent, apply_event, require, validate_event


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("command", choices=["init", "add", "observe", "prepare", "issue", "ack", "result", "reconcile", "retire", "recover", "status", "rebind", "register", "summary"])
    parser.add_argument("--input", required=True, help="JSON event body; command supplies op")
    parser.add_argument("--beads-dir", help="Explicit global Beads directory; required for new prepare/issue")
    parser.add_argument("--beads-assignee", help="Exact claimed execution actor")
    parser.add_argument("--bd-executable", help="Absolute path to the supported bd executable")
    parser.add_argument("--project-policy", help="Explicit project-bound registration and legacy-claim policy JSON")
    args = parser.parse_args()
    try:
        with open(args.input) as stream:
            event = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
        project_policy = None
        if args.project_policy:
            with open(args.project_policy) as stream:
                project_policy = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
        require(isinstance(event, dict), "event must be an object")
        event["op"] = args.command
        flags = (args.beads_dir, args.beads_assignee, args.bd_executable)
        guard = None
        if any(value is not None for value in flags):
            require(all(value is not None for value in flags),
                    "Beads guard requires --beads-dir, --beads-assignee and --bd-executable together")
            validate_event(event)
            require(event["op"] in {"prepare", "issue"}, "Beads guard supports prepare and issue only")
            guard = {"beads_dir": args.beads_dir, "assignee": args.beads_assignee, "executable": args.bd_executable}
        response = apply_event(args.db, event, guard_context=guard, project_policy=project_policy)
        print(json.dumps(response, sort_keys=True))
    except (InvalidEvent, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:500]}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
