# Prepare a native subagent

`project-factory route-subagent` is a preparation-only shared entry. It reads the
current project's existing consumer settings and root/tracker/context binding.
It never starts a worker or changes the coordinator's model, trust, approvals,
backlog, native configuration, or ownership. The native coordinator selects the
authorized bounded task and owns dispatch, liveness, writer leases and acceptance.

Invoke from the adopting project's Git root:

```sh
project-factory route-subagent --caller-id native:actual-caller --work-dir work/actual-task < work/actual-task/request.json
```

The bounded JSON input is at most 1 MiB. Its required fields are:

```json
{
  "schema": "CPI411SUBAGENTV1",
  "caller_id": "native:actual-caller",
  "task_id": "ISSUE:bounded-task",
  "project_binding": {
    "root": "/actual/project",
    "tracker": "linear",
    "project": "actual-local-project-address",
    "context": ".factory/project-context.md",
    "context_sha256": "actual-current-context-sha256"
  },
  "task": "Compact authorized bounded task, including useful verification and constraints",
  "ordinary": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"},
  "remaining_ms": 4000,
  "capability_provenance": {
    "source": "current_runtime_tool_schema",
    "tool": "collaboration.spawn_agent",
    "caller_id": "native:actual-caller",
    "captured_at": 0,
    "models": [
      {"model": "gpt-6-luna", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
      {"model": "gpt-6.1-sol", "reasoning_efforts": ["low", "medium", "high"]}
    ]
  }
}
```

Copy `project_binding` from this project's current `.factory/consumer.json` only
after checking that it matches the current root, local tracker and context. Read
the native tool's current supported models/efforts; supply those actual options,
the current caller ID and the current epoch timestamp in `captured_at`. The
example timestamp is deliberately invalid. Metadata must be no more than 60
seconds old and cannot be from the future. These fields are caller-supplied
provenance, not authenticated host/account discovery. Refresh them immediately
before dispatch; freshness is not a reusable authority grant.

The task is nonempty and at most 8192 UTF-8 bytes. The optional assessment deadline
is more than zero and at most 8000 milliseconds. Routing intersects the supplied
options with the accepted Luna/Sol worker policy: Sol low/medium/high, Luna only
its supported native efforts. An unavailable or forbidden ordinary pair refuses.

An explicit choice adds `"explicit_choice":{"model":"gpt-6-luna","effort":"low"}`.
An allowed explicit pair wins without reading credentials or calling Jev. It uses
the same binding, freshness and persisted preparation contract. A forbidden or
unsupported explicit pair refuses without silently substituting another pair.

Configure the optional private credential reference only in this project's
`jev_router.key_file` setting. Environment `TYPESAFE_API_KEY`, when present, keeps
its existing precedence. Never copy the credential into requests, evidence or
commands. A missing/unreadable credential, timeout, malformed/uncertain response,
or unavailable mapped pair returns validated ordinary arguments. A valid answer
confirming the ordinary pair is `confirmed_ordinary`, separately from `fallback`
and from a changed `selected` pair. These states do not prove usefulness or savings.

A successful envelope includes `spawn_agent`, `preparation_id`, caller/task/project
identity, capability identity and expiry, a task-owned `receipt_path`, assessment
status/overhead, and `persistence`. It starts with `dispatch:not_invoked`,
`result:not_observed`, `acceptance:not_performed`; observed model/effort and worker
usage are unknown. A mode-0600 JSON file retains that complete envelope. Root
`work/` itself and escaping or symlinked receipt paths refuse. No generic receipt
scratch directory or daemon is created.

Before launch, inspect `persistence.status`. A failed write preserves the validated ordinary fallback (or the exact explicit
choice) and the full returned envelope, with persistence status `failed`. Even a
successful changed assessment falls back to the ordinary pair if persistence fails. Save
that complete returned envelope into task-owned accepted evidence before dispatch;
if that is impossible, do not launch. Never claim a receipt was persisted because
arguments were returned.

The caller must then:

1. Recheck actual task authority, caller/project identity, live capabilities,
   native worker policy and writer ownership. If metadata expired, prepare again.
2. Invoke `collaboration.spawn_agent` with the exact returned model,
   `reasoning_effort` and `fork_turns:none`, adding the bounded task name/message.
   Record the actual tool arguments and returned worker identity in a separate
   caller-owned consumption record tied to `preparation_id` and receipt identity.
3. If sending or observing launch is uncertain, record uncertainty and reconcile
   the actual native worker's liveness. Never blind-redispatch. Preparation alone
   is neither proof of launch nor authority to duplicate it.
4. Bind the terminal result and useful produced evidence to the actual handle.
   Keep requested settings separate from host-observed settings and available
   usage. Unknown effective effort/model/usage stays unknown.
5. Independently verify the result against the original caller's acceptance.
   Record accepted/rejected/pending and evidence. Retain sanitized receipts and
   rollback evidence until acceptance and recovery no longer need them, then
   remove obsolete task-owned scratch after checking live dependencies.

The older direct module/native Archon preflight remains compatible and does not
impersonate collaboration tool metadata. Its public command checks existing
project binding; its pure `route` library performs no persistence or dispatch.
New collaboration consumers use the bound shared command rather than treating
the lower-level library as a project attestation.
