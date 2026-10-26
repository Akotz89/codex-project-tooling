"""Small JSON command interface; native requests are executed by the assistant."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .runtime import InvalidEvent, apply_event, materialize_bundle, require, validate_event


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
    parser.add_argument("command", choices=["init", "add", "refresh-references", "observe", "prepare", "issue", "ack", "result", "reconcile", "retire", "report", "project-report", "recover", "status", "rebind", "register", "summary", "bundle", "updates", "serve"])
    parser.add_argument("--input", help="JSON event body; command supplies op")
    parser.add_argument("--project-id")
    parser.add_argument("--root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--beads-dir", help="Explicit global Beads directory; required for new prepare/issue")
    parser.add_argument("--beads-assignee", help="Exact claimed execution actor")
    parser.add_argument("--bd-executable", help="Absolute path to the supported bd executable")
    parser.add_argument("--project-policy", help="Explicit project-bound registration and legacy-claim policy JSON")
    parser.add_argument("--routing-policy-provider", help="Project-root-contained provider module (serve only)")
    parser.add_argument("--destination", help="Repository-relative output directory for a verified task bundle")
    args = parser.parse_args()
    try:
        project_policy = None
        if args.project_policy:
            with open(args.project_policy) as stream:
                project_policy = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if args.command == "serve":
            require(args.host in {"127.0.0.1", "localhost"}, "API host must be loopback")
            require(args.project_id and args.root, "serve requires --project-id and --root")
            routing_provider = None
            if args.routing_policy_provider:
                try:
                    root = Path(args.root).resolve(strict=True)
                    provider_path = Path(args.routing_policy_provider)
                    if not provider_path.is_absolute():
                        provider_path = root / provider_path
                    provider_path = provider_path.resolve(strict=True)
                    require(provider_path.is_file() and provider_path.is_relative_to(root),
                            "routing policy provider must be a file within --root")
                    spec = importlib.util.spec_from_file_location("project_coordinator_bound_routing_policy", provider_path)
                    require(spec is not None and spec.loader is not None, "invalid routing policy provider")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    factory = getattr(module, "create_provider", None)
                    require(callable(factory), "routing policy provider must define create_provider(project_root)")
                    routing_provider = factory(root)
                    require(all(callable(getattr(routing_provider, name, None)) for name in
                                ("read_policy", "support", "compare_and_swap")),
                            "routing policy provider has an invalid interface")
                except InvalidEvent:
                    raise
                except (OSError, ImportError, AttributeError, TypeError, ValueError) as exc:
                    raise InvalidEvent(f"invalid routing policy provider: {str(exc)[:300]}") from exc
            from .http_api import serve
            return serve(args.db, args.project_id, str(root) if args.routing_policy_provider else args.root, args.host, args.port,
                         project_policy=project_policy, routing_provider=routing_provider)
        require(args.routing_policy_provider is None, "--routing-policy-provider is supported for serve only")
        require(args.input, "--input required")
        with open(args.input) as stream:
            event = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
        require(isinstance(event, dict), "event must be an object")
        event["op"] = args.command.replace("-", "_")
        flags = (args.beads_dir, args.beads_assignee, args.bd_executable)
        guard = None
        if any(value is not None for value in flags):
            require(all(value is not None for value in flags),
                    "Beads guard requires --beads-dir, --beads-assignee and --bd-executable together")
            validate_event(event)
            require(event["op"] in {"prepare", "issue"}, "Beads guard supports prepare and issue only")
            guard = {"beads_dir": args.beads_dir, "assignee": args.beads_assignee, "executable": args.bd_executable}
        response = apply_event(args.db, event, guard_context=guard, project_policy=project_policy)
        if args.command == "bundle" and args.destination:
            response["materialized"] = materialize_bundle(event["root"], response, args.destination)
        print(json.dumps(response, sort_keys=True))
    except (InvalidEvent, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:500]}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
