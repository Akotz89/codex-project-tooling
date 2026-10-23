# Prepare a native subagent

`project-factory route-subagent` is a preparation-only shared entry. It reads the
current project's existing consumer settings and root/tracker/context binding.
It never starts a worker or changes the coordinator's model, trust, approvals,
backlog, native configuration, or ownership. The native Core coordinator retains
global accepted-scope allocation, cross-lane coordination, and canonical Graph
authority. Within an accepted lane, the coordinator may explicitly assign a Core
lead as native coordinator for bounded children. That lead may prepare and dispatch
through the selected route, record local linked claims, own actual child handles and
writer-path leases, verify results against the assigned acceptance, and report
actual liveness and evidence to the coordinator. This bounded delegation does not
broaden or reassign project scope or create another scheduler. Execution remains
with actually routed bounded workers.

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

The task is nonempty; the combined task, native dispatch packet and any human
constraint is at most 8192 UTF-8 bytes. Supply `remaining_ms` only with a real
caller deadline; otherwise omit it or use null. Supplied budgets must be finite
and positive. The bridge does not infer a Codex deadline. Routing intersects the supplied
options with the accepted Luna/Sol worker policy: Sol low/medium/high, Luna only
its supported native efforts through max (currently low/medium/high/xhigh/max). An unavailable or forbidden ordinary pair refuses.

Actual assessment is mandatory for each new worker. Jev receives one typed Choice
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

A direct human pair requirement may constrain the actual assessment by adding:

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
the bridge reads an absolute, non-symlink regular UTF-8 file bounded to 256 KiB
and requires exact, unnormalized instruction_quote containment. It refuses
unreadable files, files owned by another non-root user and (on POSIX) group/world
writable files. Full file contents and IO error text are not logged or retained;
the supplied instruction quote remains existing receipt attribution. The coordinator still verifies applicability and actual human authorship. Chat-turn
references remain caller-checked. These checks establish source containment,
not cryptographic proof of human authorship. Missing, blank or default-source provenance refuses
before credential access. A supported constraint is included in the bounded task
passed to actual Jev, alongside the full permitted live capability catalog. It
does not preselect or restrict that catalog. Jev must actually select that exact
pair; disagreement, missing credentials or unavailable assessment refuses launch.
Success remains `selected` or `confirmed_ordinary`, with one actual attempt and
complete typed assessment evidence. Legacy `human_override` / zero-attempt receipts
cannot authorize a new child. Managed provider continuation from a legacy
unassessed receipt requires reconciliation; only assessed receipt reuse is
automated. Existing native followup_task handles are outside the spawn hook. Forbidden or unavailable pairs refuse
without substitution. The latest direct owner rule supersedes same-capability
skill conventions: neither the parent model nor a habitual default may prefilter
the Luna6 low/medium/high/xhigh/max and Sol6.1 low/medium/high intersection.

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
   `reasoning_effort` and `fork_turns:none`. For hook-covered launches consume the
   complete persisted `native_request` verbatim, including its carrier task name
   and original message; do not append a preparation marker.
   Do not replace task_name with a readable label, shorten the carrier, reword the
   message, omit fields or reconstruct the packet from the chosen pair.
   Record the native thread UUID and original tool call ID, then run the
   authoritative audit below before claiming exact consumption. A caller-written
   argument/handle record alone does not prove what the tool received.
3. After an explicit `native_capability_expired` refusal, first confirm that no
   child was created. Refresh metadata from the current native tool schema once,
   prepare a new packet and dispatch its exact `native_request` within the existing
   60-second window. An audit of an expired packet never authorizes replay.
   If sending or observing launch is uncertain, record uncertainty and reconcile
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
For a same-task continuation, resume the accepted assignment, current claim, real
worker handle/result and next acceptance; refresh only materially changed
dependencies or checks the current human explicitly requests, not full unchanged
intake.
This contract governs new bounded native handoffs; it does not start product work
or transfer dispatcher ownership.

The older direct module/native Archon preflight remains compatible and does not
impersonate collaboration tool metadata. Its public command checks existing
project binding; its pure `route` library performs no persistence or dispatch.
New collaboration consumers use the bound shared command rather than treating
the lower-level library as a project attestation.

## Versioned native task carrier

For hook-covered launches add the exact declared task to the preparation request:

~~~json
"native_dispatch": {
  "task_name": "bounded_parser_repair",
  "message": "The exact authorized task packet delivered to this worker."
}
~~~

The combined assessment context plus declared packet remains bounded to8192
UTF-8 bytes. Jev assesses that exact declared task; it does not assess ciphertext.
After successful private persistence the envelope returns a complete
`native_request` containing task_name, message, model, reasoning_effort and
fork_turns:none. Its message is the original supplied plaintext, with no appended
marker. Its `native_task_name` is `cpi414_` followed by the lowercase hex encoding
of the UTF-8 project-relative `work/<task>/.../jev-<id>.json` locator. Consume the
entire exact packet; do not hand-assemble or shorten the carrier. Failed
assessment/persistence withholds every callable packet/carrier copy.

The private mode0600 receipt now retains that complete caller packet, including
its declared plaintext. Keep it private; never publish it as generic evidence.
`native_dispatch` separately records declared task_name and message_sha256.
`native_transport` binds them to schema CPI414NATIVETRANSPORTV1, carrier
`task_name_hex_locator`, the exact relative locator and the exact carrier name.
The carrier reserves the cpi414_ prefix. It preserves Unicode filename spelling
without normalization, URL encoding or a registry. Hex permits only the native
schema's lowercase letters/digits/underscores. No advertised task_name length
limit is exposed by the pinned schema, so this contract invents none. Actual
long-name/handle acceptance remains UNPROVED until the original useful caller's
separately authorized dispatch; source tests cannot establish it.

The hook decodes only task_name. The platform may encrypt message; the hook
neither decrypts it nor searches a carrier-mode message for a plaintext marker.
It leaves observed opaque message bytes unchanged and requires the exact
persisted carrier, declared binding and assessed pair/fork packet. This surface
has no supported plaintext attestation: the declared plaintext hash is NOT an
observed opaque-message hash. Pre/Post receipts keep those hashes separate,
record plaintext_attestation:unavailable for carrier mode and require original
caller useful-result acceptance for task semantics. A matching opaque hash only
proves the same observable bytes reached these callbacks.

Historical receipts without native_transport/native_request/native_task_name
remain usable ONLY with their exact old task name and actually observable
plaintext body followed by the exact final legacy marker line:

~~~text
[CPI414 preparation=work/actual-task/jev-32lowercasehexcharacters.json]
~~~

That body must hash to the original declared message. An opaque old-marker call
cannot be reinterpreted as the new protocol; new versioned receipts cannot
silently downgrade to a marker call. Old failed/expired/consumed attempts remain
failures and are never replayed. Legacy marker spelling and final-line rules
remain unchanged. Both forms retain control-character, traversal, symlink,
private-file, task/project/caller/source and single-consumption checks. No
surrogate/control/line separators or noncanonical carrier encodings are accepted.

Do not add
a preparation field to spawn arguments. The hook accepts only a private, owned,
single-link regular JSON file below the current project's work/<task>/ tree.
Symlinks, other projects, unsafe permissions and oversized files refuse.
For a hook-covered WSL native launch, caller_id must be either the exact canonical
session UUID from the hook's session_id or native:<that same UUID>. The native:
form is the existing project preparation label, not an arbitrary namespace to
strip. Mapping is accepted only for current collaboration.spawn_agent tool
preparation provenance. Foreign UUIDs, extra prefixes and native:/root/... task
handles refuse. The hook preserves the raw event UUID and original prepared
caller separately in consumption evidence, together with the applied mapping.

The release-matched host represents PreToolUse session_id as
[ThreadId](https://github.com/openai/codex/blob/4ebc485ca321243727539187169425fcf972f2c4/codex-rs/protocol/src/thread_id.rs)
and [serializes it as a string](https://github.com/openai/codex/blob/4ebc485ca321243727539187169425fcf972f2c4/codex-rs/hooks/src/events/pre_tool_use.rs).
ThreadId uses the UUID representation; new native thread IDs are UUIDv7.
The installed bundle's input schema independently exposes session_id as a string.
This mapping deliberately accepts canonical UUIDs without requiring a version,
preserving native ThreadId compatibility.

Desktop collaboration handles such as /root/real_system_implementation are not
WSL native session UUIDs. Their host identity coverage is unsupported by this
WSL hook contract until independently established. Do not infer a UUID from a
task path, receipt location or parent preference. Source/fixture checks do not
prove installed callback invocation; the adopting owner must still verify the
actual WSL event and exact loaded definition.

The installed entry is:

~~~sh
python3 -B /actual/project/factory/native_hook.py --root /actual/project
~~~

For a global definition covering multiple adopting repositories, use the existing
shared CLI from the reviewed immutable factory publication:

~~~sh
python3 -B /immutable/factory-publication/bin/factory.py native-hook
~~~

This command has no --root override. It resolves the actual command working
project and requires the event cwd to resolve to that same canonical project,
then runs the existing native gate and its root/tracker/context/source checks.
An unsupported or unbound root yields explicit deny JSON. The same immutable
command therefore works for Setup, WoW and StratCentra only when each actual
project's current binding, receipt and event align. It never treats source
custody in another project as permission to consume its receipt. A Setup receipt
with a WoW event is refused. The project-local adapter command above remains
fixed to its own exact adopter and is not a global multi-project command.

Configure one synchronous PreToolUse matcher covering `spawn_agent`,
`collaboration.spawn_agent`, exact observed `collaborationspawn_agent`,
`functions.spawn_agent`, `multi_agent.spawn_agent` and `Agent`, and the same
bounded PostToolUse callback. Review the exact definition and runtime script bytes before adopting. The flat collaboration name
is the observed namespace+name serialization, not permission to normalize other
namespaces or session identities. Use hooks/list currentHash
and supported trust/reload APIs. Installation ships this entry but does not change
hooks, trust or global configuration. A 10-second callback deadline is appropriate
only after validating source-check duration on that host.

PreToolUse requires matching cwd/project, caller/session and exact versioned
declared-task/carrier binding (or exact historical plaintext marker body),
current capability snapshot (at most60seconds),
successful private receipt persistence and a permitted assessed pair. It reserves
jev-<id>.consume.json exclusively before returning supported allow plus whole
updatedInput with exact assessed model/effort and fork_turns:none, preserving
task name/message. An existing reservation refuses another launch, including
replay of the same session/turn/tool-use id. Missing preparation, unsupported
arguments and caught failures return supported explicit deny with a nonempty
stable sanitized reason such as native_carrier_invalid,
native_transport_binding_mismatch, native_packet_pair_mismatch,
native_capability_expired or native_dispatch_already_reserved. No raw task,
opaque message, credential or arbitrary exception body is emitted. Receipt
preparation alone never proves launch.

PostToolUse requires the exact supplied model, reasoning_effort and fork_turns to
match the prepared pair. It hashes the supplied whole argument object without
rewriting conflicting values, then checks that hash and the same
session/turn/tool-use id against the original reservation before writing
jev-<id>.result.json with an actually reported agent_id/agentId or
explicit unknown result. A post callback may arrive after capability expiry;
reserved identity, declared-task/transport binding and exact applied opaque
arguments must still agree. Carrier-mode PRE already requires the supplied pair
and fork policy to match the complete packet; it does not substitute conflicting
caller settings. Historical plaintext mode keeps its assessed-pair rewrite. It never invents effective model/effort/usage
or caller acceptance. Malformed/missing post evidence requires reconciliation.
Conflicting or missing applied settings produce reconciliation context without a
handle receipt; the original pending reservation remains unchanged. Uncertain
sends retain their reservation and never authorize blind redispatch.
Useful terminal result and original-caller acceptance remain caller-owned.

**Host limitation:** callback launch errors, timeout, malformed output, untrusted
or changed definitions and adapter-loader failures can fail open in the vendor
host. Caught policy failures deny; a process which never reaches the handler
cannot return denial. Definition trust hashes configuration, not referenced
script bytes. Read back active trust/enabled hashes, pin script content and prove
actual unprepared denial plus next necessary assessed rewrite/handle/useful
result for each used native path. Missing enforcement evidence is an incident or
coverage gap. Source tests do not establish universal technical fail-closed
enforcement.

## Audit the original native call

From the actual bound caller project's Git root, invoke the existing shared entry:

~~~sh
project-factory audit-native-call --preparation work/actual-task/jev-32lowercasehexcharacters.json --thread-id actual-canonical-thread-uuid --call-id actual-native-call-id
~~~

The existing usage collector resolves only that exact thread through current
`CODEX_HOME/state_5.sqlite` in read-only mode. It requires the authoritative state
row's cwd and the owned rollout's first session_meta identity/cwd to match the
bound caller project. Only a safe original rollout below that Codex home's
sessions or archived_sessions is accepted. Caller-supplied transcript paths and
self-reported consumption receipts cannot substitute for these original records.
The audit strictly decodes bounded JSON envelopes to select the decoded call ID,
including escaped identities. It examines argument/output contents only for the
requested call, with no semantic inspection or retention of unrelated contents.
Oversized, duplicate-key, incomplete or concurrently changed source records refuse.
It refuses ambiguous, missing or malformed evidence and never dispatches or writes preparation, reservation or
result files. Its fixed JSON result contains source/record/argument hashes,
matching status and mismatched field names; it withholds task/message/output
contents and arbitrary exception text. The reported handle is retained as a
SHA-256 identity hash, separately from terminal result and caller acceptance.

Exit 0 means the original result reports a supported worker handle and either
all original plaintext arguments match the private packet or the readable
carrier/model/effort/fork fields match with consistent existing V2 PRE/POST hook
evidence for the opaque stored message. Opaque success requires matching original
thread/call, project/task/preparation, native transport, canonical hook argument
hashes, observed message bytes and reported handle. Missing or inconsistent
private hook records leave opaque launch evidence unverified. The audit never
creates, changes or replays hook records. `observable_arguments_match` describes
the fields that can be compared; `arguments_match` stays null and
`plaintext_attestation` stays unavailable for opaque messages. It does not
establish effective model/effort, terminal success or useful-result acceptance.
Mismatch, explicit native refusal, absent result and unverified evidence exit
nonzero. An opaque message is never listed as mismatched merely because its hash
differs from the declared plaintext. A changed readable task_name remains a
proven mismatch; an expired native refusal remains a refusal without requiring
invented hook evidence. `fresh_at_call` reports whether the preparation was fresh at the original
call timestamp. Historical evidence may remain valid after expiry; the result
always sets `replay_authorized:false`. Declared plaintext hashes, actual stored
message hashes, raw original argument/record hashes, and canonical hook hashes
remain separate. Compact normalized audit argument hashes use a different
serialization from the hook's `SHA256(json.dumps(args, sort_keys=True).encode())`
with its default whitespace and ASCII escaping. Matching observed opaque bytes
never establishes plaintext equality or useful-result acceptance.

Wait on an existing collaboration worker using the current supported native
`collaboration.wait_agent` arguments, such as `{"timeout_ms":30000}`. That tool
has no `target` argument. Use `list_agents` for live worker inventory, reconcile
uncertain sends against the existing worker, and bind its useful terminal result
to the original caller's acceptance before recording success.

Historical auditing retains the current bound source and policy checks. A recorded
packet must remain compatible with that current binding; validation failure returns
`current_binding_unverified`. There is no historical source or policy bypass.
`preparation_sha256` hashes the exact safely read private preparation file bytes,
including its whitespace, separately from normalized argument hashes.
