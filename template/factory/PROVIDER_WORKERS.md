# Prepare an actual provider worker

The same factory preparation service supplies mandatory persisted assessment at
an Archon logical-worker boundary:

~~~sh
project-factory route-worker --caller-id archon:<run-id> \
  --work-dir work/archon-worker-admission/<run>/<taskhash> < request.json
~~~

Run from the canonical adopting Git project. The service checks current local
settings, tracker/context/root binding and immutable Archon source before
selection. Request schema is CPI414PROVIDERWORKERV1 with caller_id, task_id,
project_binding, task, ordinary, optional remaining_ms, catalog and catalog_provenance.
Task is the exact bounded final prompt, at most 65536 UTF-8 bytes without
truncation; native subagent limits remain 8192. Ordinary is contextual
{provider:"codex",model,effort}, never a bypass. `remaining_ms` may be omitted
or null to use the stdlib transport default and parent cancellation, or supplied
as a finite positive millisecond budget. The bridge enforces only an explicitly
supplied budget; it does not infer a Codex deadline.

Catalog is the actual result from the same authenticated app-server connection
which will create the worker, normalized as:

~~~json
{
  "same_account": true,
  "identity": "<nonsecret-connection-uuid>",
  "fetched_at": 0,
  "models": [{
    "model": "gpt-6-luna",
    "supportedReasoningEfforts": [
      {"reasoningEffort": "medium", "description": "Actual catalog description"}
    ]
  }]
}
~~~

The companion provenance is exactly
{source:"codex_app_server_same_connection",connection_id:"<same uuid>",captured_at:0}.
Identity and timestamp agree and are at most 60 seconds old. This typed provenance
is supplied by the trusted provider integration; Python does not authenticate an
arbitrary external caller's assertion. Never derive identity from a credential.
Policy permits only Luna low/medium/high/xhigh/max and Sol6.1 low/medium/high,
intersected with the current catalog.

Success is selected or confirmed_ordinary with exact model, effort, preparation_id,
receipt_path, verified source_binding and persistence.status persisted. Both need
one actual Jev attempt and complete typed selection evidence. A human instruction
is an assessment constraint; it never bypasses Jev. A different selected pair
refuses launch rather than silently replacing the constraint. Other
outcomes refuse with no launch-ready pair. A persistence failure removes top-level
model/effort and every callable spawn_agent, dispatch_receipt and
spawn_agent_request copy, including nested copies, while preserving assessment
pair evidence and marking spawn arguments unavailable. Optional CLI flags, ordinary config,
connected accounts and generated choices do not authorize bypass. The existing
private credential reference and single Jev transport are reused.

A provider explicit_choice also requires caller-checked attribution metadata:

~~~json
"human_attribution_verification": {
  "checked": true,
  "checked_by": "<actual caller who checked the human source>",
  "checked_at": "2026-10-20T12:00:00-05:00"
}
~~~

These are exactly three fields. checked must be literal true; checked_by is a
nonblank bounded actor string without control characters; checked_at is an ISO
timestamp with an explicit timezone. The actual verifier may differ from the
logical archon:<run-id> caller. The trusted typed workflow API must verify actual
source and quote applicability before supplying these fields; the factory does
not authenticate arbitrary caller assertions or prove human authorship.
Missing/false/malformed verification refuses before credentials or assessment.
Successfully assessed human constraints persist the exact metadata both at the envelope's
human_attribution_verification and receipt.human_attribution_verification.
The native route-subagent attribution checks remain separate; both routes require actual assessment.

Await receipt plus durable logical-task consumption metadata before thread/start
or task-creating fork; preserve transport uncertainty. Provider owns its fsynced
admission pointer and dispatch/session state in the task directory. There is no
second factory index, scheduler or dispatcher.

For a verified same logical task send continuation:true plus
--reuse-receipt <exact original receipt_path>, same caller/task/project identity
and fresh catalog. It returns the original preparation/pair without reassessment,
even when a continued prompt or physical fork differs. Original assessment does
not expire merely because the task continues; current source, binding, private
receipt and selected pair availability are rechecked. Missing/mismatched/unsafe
receipt, changed original source pin or unavailable pair refuses with
reconciliation and no substitution.
Reuse alone never authorizes another worker after uncertain send. Distinct new
nodes, reviewers, children or fresh tasks need new assessment. Execution metadata
binds actual handle, terminal result and caller acceptance separately. A constrained
continuation retains the original checked attribution; changed supplied
verification or choice cannot replace the original preparation. Unknown
effective telemetry stays unknown.

Receipt directories use the same project-local path contract as native
preparations: legitimate Unicode, plus, parentheses and spaces are preserved
exactly. Control characters (C0/C1), Unicode line/paragraph separators and
surrogate scalars refuse before credentials, assessment or persistence. Existing
root, symlink, private-file and same-task reuse checks remain required.

The provider gate applies to direct/default NEW worker calls and title-purpose
threads as well as workflow nodes; a missing logical identity or either awaited evidence
callback refuses before thread creation. The workflow adapter refuses providers
without this actual admission contract rather than silently launching them.
Legacy zero-attempt preparations cannot be reused to mint a child. This gate does
not change coordinator model settings. Only source-level mocked controls are
claimed here; the adopting owner verifies its loaded runtime and host callbacks.
Safe provenance records the endpoint/model, credential source reference (an env
name or file reference only), returned request ID/status when available and token
usage. It does not infer account identity, balance, debit or effective effort.

Human orchestrator chats explicitly carry purpose user-lead and typed existing
conversation/session provenance from orchestrator_user_input. They preserve the
original human lead model, effort and native session; absent worker metadata
alone never implies this role. A workerTask, node identity (including blank),
fork intent or worker callback contradicts lead classification and refuses.
Trusted caller classification is not cryptographic verification of arbitrary
API callers. Titles are separate workers: the central service supplies a new
conversation/invocation identity and awaited fsynced host evidence in the existing
admission directory. Only Codex title worker admission is supported; other title
providers refuse before sendQuery with the existing visible title fallback.
Unbound projectless/workspace title calls require an actual routing binding;
this correction does not adopt a binding or borrow another project's identity.
