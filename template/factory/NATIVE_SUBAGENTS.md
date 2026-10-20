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
  "task": "Current authorized goal, known facts and ambiguities, risk and safeguards, dependencies/interfaces, acceptance and useful verification",
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

Each model row may optionally include `description`, copied from the current
native tool's model description. When current tool metadata supplies descriptions
for individual efforts, use the explicit optional `reasoning_effort_descriptions`
mapping from an advertised effort name to its description:

```json
{
  "model": "gpt-6-luna",
  "description": "Actual current native tool model description",
  "reasoning_efforts": ["low", "medium"],
  "reasoning_effort_descriptions": {
    "low": "Actual current native tool description for low effort"
  }
}
```

The router forwards these descriptions to Jev in both the allowed catalog and
the typed pair options. Descriptions are optional strings, redacted before a
256-character limit per model/effort description. Absent, blank or non-string
descriptions are ignored; a non-object effort-description mapping is ignored.
Mapping entries cannot advertise extra efforts: only entries corresponding to
an advertised, policy-permitted pair can reach Jev. Copy current metadata when
available; do not invent descriptions, prices, benchmarks or rankings. Descriptors
remain caller-supplied context, and never expand capabilities or task authority.

The task is nonempty and at most 8192 UTF-8 bytes. The assessment deadline
is more than zero and at most 8000 milliseconds. Routing intersects the supplied
options with the accepted Luna/Sol worker policy: Sol low/medium/high, Luna only
its supported native efforts through max (currently low/medium/high/xhigh/max). An unavailable or forbidden ordinary pair refuses.

Assessment is the default for each new worker. Jev receives one typed Choice
whose options are exactly the current capability/policy intersection plus explicit
`insufficient_information` and `uncertain` outcomes. It selects a model/effort pair
directly; there is no four-pair category matrix or remapping to the ordinary pair.
Supply current goal, ambiguity and known facts, risk and safeguards, relevant
dependencies/interfaces and observable acceptance in `task`. No special task
headings are required. The ordinary pair is fallback context, never an instruction
to choose it. A normal habitual default cannot be labeled a human override.

A valid answer selecting the ordinary pair is `confirmed_ordinary`; a changed
pair is `selected`. Both require one actual assessment and preserve its exact
pair. Confidence and complete option probabilities are retained as provider
evidence, without claiming calibrated suitability, optimality or savings. A low
preference confidence across suitable adjacent efforts alone does not refuse a
unique valid choice. Tied maximum probabilities, explicit insufficient/uncertain
context, malformed or unsupported answers refuse.

A direct human pair requirement may bypass assessment by adding:

```json
"explicit_choice": {
  "model": "gpt-6-luna",
  "effort": "max",
  "source": "direct_human_instruction",
  "instruction_quote": "Use gpt-6-luna at max for this bounded worker.",
  "instruction_reference": {
    "kind": "chat_turn",
    "chat_id": "actual-chat-id",
    "turn_id": "actual-human-turn-id"
  }
}
```

The exact nonempty instruction quote and a reference are mandatory. A chat/turn
reference must identify the actual human source the coordinator can resolve; the
bridge checks identifier shape, and the coordinator checks the source and quote.
Alternatively use `{"kind":"governing_instruction","path":"/absolute/existing/instruction/file"}`;
the bridge verifies that this file exists and the coordinator verifies its
applicability and exact quote. This is caller attribution, not cryptographic
proof of human authorship. Missing, blank or default-source provenance refuses
before credential access. Valid supported overrides use status `human_override`
and assessment status `unassessed_human_override`, read no credentials and call no
Jev service. They still require current binding, capabilities and persisted receipt.
Forbidden or unavailable pairs refuse without substitution.

Configure a private credential reference in this project's `jev_router.key_file`
setting. Environment `TYPESAFE_API_KEY`, when present, keeps its existing
precedence. Never copy credentials into requests, evidence or commands. Missing or
unreadable credentials, unavailable transport, timeout, insufficient context,
malformed/indeterminate/unsupported answers return `refused` with no launch-ready
arguments. The bound command persists the sanitized failure reason when its
receipt store is available. A failure never silently prepares Sol-medium.

A successful envelope includes `spawn_agent`, `preparation_id`, caller/task/project
identity, capability identity and expiry, a task-owned `receipt_path`, assessment
status/overhead, and `persistence`. It starts with `dispatch:not_invoked`,
`result:not_observed`, `acceptance:not_performed`; observed model/effort and worker
usage are unknown. A mode-0600 JSON file retains that complete envelope. Root
`work/` itself and escaping or symlinked receipt paths refuse. No generic receipt
scratch directory or daemon is created.

Before launch, require `persistence.status == "persisted"`. A failed receipt
write returns `refused`, `persistence.status:"failed"` and no `spawn_agent`,
`dispatch_receipt` or nested launch-ready argument copies, for both assessed
choices and human overrides. It preserves the actual chosen pair as evidence;
it never replaces it with the ordinary pair. Restore persistence and prepare
again before dispatch. Never claim a receipt was persisted because assessment
succeeded. Preparation alone does not authorize launch.

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

Existing workers and same-worker continuations keep their current settings.
This contract governs new bounded native handoffs; it does not start product work
or transfer dispatcher ownership.

The older direct module/native Archon preflight remains compatible and does not
impersonate collaboration tool metadata. Its public command checks existing
project binding; its pure `route` library performs no persistence or dispatch.
New collaboration consumers use the bound shared command rather than treating
the lower-level library as a project attestation.
