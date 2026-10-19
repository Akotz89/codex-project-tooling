"""Small JSON command interface; native requests are executed by the assistant."""
import argparse
import json
import sys

from .runtime import InvalidEvent, apply_event, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("command", choices=["init", "add", "observe", "prepare", "issue", "ack", "result", "reconcile", "recover", "status", "rebind", "summary"])
    parser.add_argument("--input", required=True, help="JSON event body; command supplies op")
    args = parser.parse_args()
    try:
        with open(args.input) as stream:
            event = json.load(stream)
        require(isinstance(event, dict), "event must be an object")
        event["op"] = args.command
        print(json.dumps(apply_event(args.db, event), sort_keys=True))
    except (InvalidEvent, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:500]}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
