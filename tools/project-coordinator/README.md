# Persistent project coordinator

Local LangGraph StateGraph (`transition → readiness`) with synchronous SqliteSaver checkpoints and one exclusive flock per database. Linear owns plan/scope/dependencies; these inputs are observations from the coordinator. Beads references are mandatory. No daemon, native app endpoint, LLM, or provider calls.

Run `uv sync --locked` then `project-coordinator --db work/<run>/state/coordinator.sqlite COMMAND --input EVENT.json`. Every command outputs compact JSON; errors exit 2. Import `project_coordinator.apply_event(db,event, *, guard_context=None, project_policy=None)` with the same contract plus `op`; new prepare/issue transitions require the explicit guard context below.

## Exact event contract

Every mutating command requires a unique `event_id` string. Identical event IDs and canonical payloads return REPLAY; altered payloads with the same ID are rejected. A replay never exposes native arguments again. Status/summary/recover need no event ID.

`init`: `{ "event_id":"init-1", "project": { "project_id": "P-example", "root": "/absolute/canonical/project", "linear_ref": "https://linear.app/...", "beads_ref": "global:execution-id", "coordinator_thread_id": "actual-root-thread", "workers": {"implementer":"implementation-thread", "reviewer":"review-thread"} } }`. The nonempty worker map is project supplied; every key must match the safe role-name pattern and every thread ID must be distinct.

All other commands require common binding `{ "project_id":"P-example", "root":"/absolute/canonical/project" }` plus:

| Command | Additional fields |
| --- | --- |
| add | `task:{id,linear_ref,beads_ref,worker,prompt,source_digest,scope_digest,dependencies:[],writer_paths:[],references?:[]}`; paths project-relative or absolute within root; empty list means read-only |
| refresh-references | `task_id`, exact `source_digest` and `scope_digest`, prior `expected_reference_revision` and `expected_reference_digest`, new `reference_revision`, and complete replacement `references` list; coordinator-only compare-and-swap for existing tasks |
| observe | `worker`, `state:"IDLE" / "BUSY" / "UNKNOWN"`, `evidence:"source observation reference"` |
| prepare | `task_id`, `source_digest`, `scope_digest` matching immutable task identity |
| issue | `task_id`, `dispatch_id` returned by prepare |
| ack | `task_id`, `dispatch_id`, `receipt:{path,sha256}` pointing at actual native MCP acknowledgement JSON |
| result | `task_id`, `dispatch_id`, `receipt:{path,sha256}` pointing at normalized result below |
| reconcile | `task_id`, `dispatch_id`, `quiescent:true`, `evidence:"observed stopped/no pending send reference"`; abandons PREPARED/UNKNOWN/ACKNOWLEDGED/FAILED work as FAILED and releases lease; late results for a released dispatch are rejected |
| retire | `task_id`, exact `source_digest` and `scope_digest`, `quiescent:true`, `evidence`; only a never-dispatched WAIT/READY task with no dispatch binding or lease can become terminal RETIRED |
| register | `worker`, `new_thread_id`, the complete `expected_workers` map, and `evidence`; additive compare-and-swap registration requires an explicit project policy |
| rebind | `worker`, `expected_old_thread_id`, `new_thread_id`, `evidence`; compare-and-swap replacement, requiring no held/uncertain dispatch and no observed BUSY worker |
| summary | common binding; optional task `offset`/`limit`, task-report `report_offset`/`report_limit`, and project-report `project_report_offset`/`project_report_limit` (each limit defaults to50 and is capped at100) |
| bundle | common binding and `task_id`; optional `role` plus exact `thread_id` for an assigned worker |
| updates | common binding, exact `role` and `thread_id`, and optional `after_revision` |
| report | `task_id`, bound `reporter` worker role and `reporter_thread_id`, `kind`, timezone-aware ISO `occurred_at` (at most 64 characters and no more than five minutes ahead of server receipt; stored as canonical UTC), `state` (at most 512 characters), `detail`, a `source` link, `evidence` links, and exact `claim_id`; optional `dispatch_id` must match the bound task |
| project-report | `work_item` reference, exact registered `reporter` and `reporter_thread_id`, `kind`, timezone-aware ISO `occurred_at`, `state`, `detail`, a `source` reference, and 1–16 evidence links; a project-scope observation does not change task ownership/status, dispatch state, or Root acceptance |
| status / recover | common binding only; existing full response/recovery semantics |

Every input is a JSON object, not a JSON string. CLI command supplies `op`. Inputs and receipts must contain no credentials or full logs.

`report` appends owner-attributed progress to the coordinator's existing checkpoint model. Identical event IDs replay without another row; conflicting reuse fails. Each stored row includes its event time and server recording time. The bounded summary returns up to 100 reports newest-first and each returned task's latest event-time report; late reports stay attributed in history and are marked stale without replacing newer projection state. `report_offset` and `report_limit` page through older reports; bounds include previous/next offsets and the total report count. Submission is optional telemetry: producers should surface failures and retry the same event but never block task execution or convert reported results into Root acceptance.

### Linked task bundles and role-filtered updates

`task.references` may carry repository-relative source, schema, config, test, artifact, evidence, or cleanup references. Each reference has `id`, `kind`, `path`, `sha256`, `revision`, `updated_at`, `required`, and `dynamic`, with optional typed `depends_on` edges and a `dependency_status` of `known`, `unknown`, `dynamic`, or `unavailable`. The coordinator resolves paths under the bound repository root and reports `current`, `missing`, `changed`, `dynamic`, or `unavailable`. It rejects absolute and parent-traversal reference paths and symlinks anywhere in a declared reference path. Python imports, explicit resource reads, schema `$ref`s, and test edges can be declared without copying source bytes into coordinator state.

`bundle` returns the requested task plus its complete transitive prerequisite closure, all declared references and a typed graph from the same SQLite/LangGraph checkpoint. It reports missing task IDs and required reference drift explicitly. Legacy tasks with no indexed references are marked `source_coverage: unindexed`; their bundle is not presented as current. The `refresh-references` event attaches a complete reference index to an existing task without changing its dispatch, claim, status, or acceptance history. It compares source/scope digests and the previous reference revision/digest before replacing the index. There are no fixed task, reference, per-file, or aggregate byte ceilings; hashing, materialization, and local downloads stream in chunks over the actual declared inputs. A task's `source_digest`, `scope_digest`, Linear and Beads identities, role, dispatch, writer paths, current revision, and freshness remain visible. The bundle does not copy source bytes into coordinator state.

For safe local materialization, pass a repository-relative destination with the CLI `--destination` flag. Only references whose current bytes match their pinned SHA-256 are copied; required changed, missing, dynamic, or unavailable inputs fail closed. Optional unknown or dynamic references stay visible and are skipped. Existing destination files are accepted only when their bytes already match. Both source and destination are opened by no-follow directory descriptors; materialization removes only files/directories created by that invocation if a later source changes or fails verification.

`updates` is a read-only long-poll building block for native callers: it compares the caller's saved revision with task revisions and returns only changed tasks assigned to that exact role/thread. Cross-role project observations are included only when their `work_item` names one of that role's tasks. The coordinator can direct a project report to registered PO/SM identities with an explicit `recipients` list; each recipient sees only the latest directed update for a task bound to its exact role/thread. Such recipients can open only that directed task bundle and its declared evidence. The coordinator advances its revision once for each new event ID; a replay does not advance it. The response's `changed` flag is false for an empty, repeated, or unrelated update, so callers can avoid waking an LLM. There is no scheduler or second event feed.

The authenticated API exposes owner-only `GET /api/graph` and `/api/status`, task bundles, declared-reference downloads, and role-bound `GET /api/updates?role=<role>&thread_id=<thread>&after_revision=<revision>`. Worker tokens can read only their assigned task bundle, its declared references and relevant updates; registered project roles can read only bundles explicitly directed to that role and relevant updates. Writes are POST-only and use the same idempotent report path. Owner and participant bearer tokens must be distinct. The reference route resolves only a declared task/reference identity, opens the in-root file without following symlinks, verifies its SHA-256, and streams a verified temporary attachment as `text/plain` with `nosniff`; it never serves arbitrary workspace paths or executable HTML. The root browser page renders the paged board, typed graph, task/reference drilldown, event freshness and reports. Graphs show the latest task evidence summary; the complete report timeline remains available through the paged summary. Local evidence is downloadable only when declared for the task; undeclared local references stay visibly unlinked. The `bundle` route returns the complete dependency closure; browser pagination applies to the board and graph.

Participant writes use `POST /api/participant/report` with a role-specific bearer token configured only in the service environment as `PROJECT_COORDINATOR_PARTICIPANT_TOKENS` JSON mapping role to secret. A write body contains task, claim, dispatch and source digests plus an ACK/progress/blocker/result/lifecycle/handoff observation. The API derives reporter role/thread from the token and current project binding, then checks task owner, exact claim, dispatch and source identity before using the existing idempotent `report` event. Tokens shorter than 24 characters, duplicate tokens, unknown roles and mismatches are refused. If the role-token map is unset, the existing global bearer remains read-only. Do not expose these secrets in the browser, request logs, saved files or evidence. The package does not provide a saved model/effort settings editor; preserve the configured routing policy through its existing owner-controlled interface.

`project-report` provides optional project-scope telemetry for all three registered identity paths: existing workers using their exact `project.workers` role/thread binding, `coordinator` using the existing `coordinator_thread_id`, and separately configured project-only role/thread pairs. It requires a real work-item reference plus evidence links and stores separately from task-owner reports. `project_report_offset` and `project_report_limit` page the query-only summary and browser view. Existing checkpoints without project observations remain compatible; missing `project_reports` is treated as empty. Reuse the exact event and `event_id` on retry. No producer wiring, chat attribution, report-failure gate, or acceptance signal is added.

The query API and browser view run explicitly against the existing configured database and project binding:

```text
PROJECT_COORDINATOR_TOKEN='<at-least-24-character-secret>' project-coordinator --db work/<run>/state/coordinator.sqlite serve --project-id P-example --root /canonical/project --host 127.0.0.1 --port 8765
```

The service binds only to loopback and requires bearer authentication. Database path, project ID and root come from process configuration, never the request. The root page is a reusable static view; its password field stays in memory and is not saved to browser storage. No cross-origin access is enabled. Owner status/graph and all participant bundle/reference reads use the same query-only coordinator state; the API does not create or migrate a database. Participant report routes accept POST only.

For a Chromium browser on Windows with Node.js 22 or newer, `open_status_browser.cjs` launches or reconnects to the caller-supplied isolated profile and re-authenticates the status page. It accepts only the caller's explicit HTTP root URL on `127.0.0.1`; it verifies the unauthenticated coordinator response, exact page title and controls, selected loopback DevTools endpoints, and the browser's exact `--user-data-dir` before reading the bearer token from stdin. If the verified browser has no matching status tab, it opens one there and waits for a successful checkpoint/project render; a visible error or render timeout fails. Re-authentication of an already successful page leaves its visible content unchanged. Any mismatch fails closed. Never put the token in command arguments, environment variables, URLs, browser storage, logs, or evidence:

```text
ssh <windows-host> 'node C:/path/to/open_status_browser.cjs --url "$COORDINATOR_URL" --profile C:/path/to/private-profile --chrome "C:/Program Files/Google/Chrome/Application/chrome.exe" --debug-port 9333' < /path/to/private-token
```

Set `COORDINATOR_URL` in the Windows Git Bash session to the exact caller-supplied HTTP loopback root URL. The helper's unauthenticated preflight sends no Authorization header and omits credentials; it requires HTTP 401 JSON `{"error":"unauthorized or unknown route"}`. It uses the token only in a loopback authenticated request and the page's in-memory password field, and never persists it. Its success output omits the token and project data.

Task `report` submission still requires the exact bound task owner and its exact worker thread binding. Project `project-report` submission accepts an existing worker only with its exact `project.workers` role/thread binding, `reporter:"coordinator"` only with the exact `coordinator_thread_id`, or a separate project role/thread pair from the optional `project_report_registrations` project-policy field. These project-only roles stay outside `project.workers`, so they do not enter the dispatch map or count as task owners. This package does not wire callers, install hooks, or read chat history. Producers should submit only real work-item/evidence references using the observed event time. Reuse the exact JSON and `event_id` on retry; keep work running if report delivery is offline. All reports describe observations and do not confer Root acceptance.

`retire` preserves the task and its original source/scope identity, records the event and evidence reference, and leaves dependents blocked because only COMPLETE satisfies a dependency. It rejects any task outside WAIT/READY or with a dispatch ID, pinned dispatch thread, or lease. The event must match the bound project/root and exact task digests, and explicitly set `quiescent:true`; the caller must ensure its evidence reference supports that claim. Retired IDs cannot be prepared or issued again, and `add` cannot replace them; replacement work uses a distinct task ID.

Canonical retirement is Root-controlled. Construct the input from the current authoritative queued state and run it only against the existing canonical coordinator database:

```text
project-coordinator --db work/<run>/state/coordinator.sqlite retire --input work/<run>/retire.json
```

The input contains the existing `project_id` and canonical `root`, the queued `task_id`, its exact `source_digest` and `scope_digest`, `quiescent:true`, a unique `event_id`, and a nonempty `evidence` reference. For example:

```json
{
  "event_id": "retire-<unique-id>",
  "project_id": "<bound-project-id>",
  "root": "<canonical-project-root>",
  "task_id": "<queued-task-id>",
  "source_digest": "<current-task-source-digest>",
  "scope_digest": "<current-task-scope-digest>",
  "quiescent": true,
  "evidence": "work/<run>/retirement-evidence.json"
}
```

The candidate tests use isolated temporary databases; they do not apply retirement to the canonical graph.

## Mandatory live Beads ownership and lease guard

For new work, use the guarded `prepare` and `issue` path with the same three explicit flags on **both** commands:

```text
scripts/project-coordinator --db work/<run>/state/coordinator.sqlite prepare --input PREPARE.json --beads-dir /absolute/global/.beads --beads-assignee 'codex:<coordinator-thread>:<task>' --bd-executable /absolute/path/to/bd
scripts/project-coordinator --db work/<run>/state/coordinator.sqlite issue --input ISSUE.json --beads-dir /absolute/global/.beads --beads-assignee 'codex:<coordinator-thread>:<task>' --bd-executable /absolute/path/to/bd
```

The linked execution record must already have been claimed through the supported Beads operation and read back. Its exact `assignee` is the execution actor; the Beads `owner` field is not an executor claim. The task's `beads_ref` accepts `global:<exact-id>` or a canonical `global-...` ID. No search, prefix lookup or automatic record creation is used.

At the public `apply_event` mutation boundary, a prepare attempt without an existing dispatch or an issue attempt on a PREPARED task requires guard context, except for event replay. This also applies when readiness currently reports WAIT: external readiness can change between the preview and graph commit, so the guard checks stable dispatch state under the lock. CLI forwards its three flags; Python callers supply the same explicit mapping:

```python
apply_event(db, event, guard_context={
    "beads_dir": "/absolute/global/.beads",
    "assignee": "codex:<coordinator-thread>:<task>",
    "executable": "/absolute/path/to/bd",
})
```

Under the existing exclusive database lock and before checkpointing the event, the guard reads the task binding through query-only summary and executes `bd --readonly --global show <exact-id> --json` twice, setting `BEADS_DIR` to the supplied directory and `BEADS_DOLT_SHARED_SERVER=1`. Both observations must have exactly one matching ID, exact task `linear_ref`, `in_progress` status and exact supplied assignee. Identity, ownership, status, Linear binding, revision (when returned), heartbeat and expiry must agree between observations. The final observation must have timezone-aware parseable `heartbeat_at` and `lease_expires_at` satisfying `heartbeat_at <= current time < lease_expires_at` and `heartbeat_at < lease_expires_at`. No arbitrary lease lifetime or heartbeat age limit is added.

Legacy records without heartbeat and expiry qualify for `LEGACY_LEASELESS` only when the exact Beads ID and assignee pair appears in the explicitly supplied project policy. The policy must match both the bound `project_id` and canonical `root`; there are no generic legacy-ID or actor defaults. Any partial, malformed or expired lease refuses dispatch, including an otherwise authorized legacy pair.

Missing guard context, unclaimed/transferred/closed records, malformed output, expired/future/invalid lease evidence, backend errors and ambiguous/truncated bindings stop a new reservation/request before checkpointing. Pending coordinator checkpoints require explicit `recover` before `prepare` or `issue`; those commands never implicitly resume pending input. All three CLI flags are required together and accepted only for `prepare` and `issue`. Python guard context requires exactly the three documented keys. REPLAY, REUSE and RECONCILE outcomes for existing work require no new guard and never expose a new native envelope. Ack/result/recover/retire and normal status/summary preserve their existing history and leases without Beads lookup.

A new guarded prepare pins the verified Beads ID, exact assignee and Linear reference as internal `beads_claim` evidence through the existing model input channel, using a copy of the pre-transition model. The external event and its replay hash remain unchanged. The input checkpoint contains the pin before preparation runs; interruption recovery retains it rather than returning an unbound reservation as safely issuable. A WAIT attempt can retain an observation but has no reservation; a later new prepare pins its own newly verified identity. An existing reservation's pin is never replaced by prepare/replay.

Issue compares its fresh verified identity against that reservation pin. A transfer to another actor refuses issue even if that new actor supplies matching live guard context. Same-owner heartbeat/expiry/revision renewal can pass because those changing values are rechecked live, rather than pinned as identity. Historical PREPARED entries without a pin fail closed for new issue and retain their lease/history pending explicit Root reconciliation; recovery does not invent a binding or release them. No new graph schema, version or checkpoint node is introduced. The remaining external observation-to-commit race is unchanged.

A successful new reservation/issue adds compact `beads_preflight` evidence with coordinator checkpoint ID, UTC heartbeat, expiry and observation time, plus `LEASED` or `LEGACY_LEASELESS` mode. This reports stored renewal evidence; this read-only guard never renews or mutates Beads. The exact native owner performs supported heartbeat/renewal and verifies the resulting expiry externally. The guard creates no graph schema, scheduler or mirrored tracker and sends no native request itself.

The native local store remains trusted within the existing tool boundary; these observations are not cryptographic authentication. The coordinator lock cannot make Beads and the graph atomic: ownership may transfer after the final native observation and before graph commit. Every new `issue` checks again, and callers must still honor workers, writer leases and accepted BMAD/Linear scope.

`prepare` durably reserves PREPARED, returning its dispatch ID. `issue` writes ISSUING synchronously before returning one `native_request:{tool,arguments}` for assistant execution. Execute its exact prompt/target with the supported native tool, save the actual tool response, then `ack`. A supported acknowledgement can be just `{"threadId":"..."}` or MCP `content:[{type:"text",text:"{\"threadId\":\"...\"}"}]`; no unavailable call ID is required. A queued acknowledgement only means ACKNOWLEDGED.

Result receipt is a small normalization of actual retained native/child evidence:

```json
{
  "thread_id":"actual-worker-thread",
  "dispatch_id":"prepared-id",
  "source_digest":"bound-source-identity",
  "scope_digest":"bound-scope-identity",
  "terminal":true,
  "quiescent":true,
  "child_exit_code":0,
  "acceptance":true,
  "artifacts":[{"path":"work/<run>/output.json","sha256":"actual-64-char-sha256","size_bytes":123}],
  "native_evidence":{"path":"work/<run>/native-final.json","sha256":"actual-64-char-sha256"}
}
```

The hashed native final evidence must include the target thread and exact dispatch marker. The coordinator/operator checks its meaning and faithfully normalizes terminal quiescence, explicit child outcome, acceptance and artifact claims; this runtime verifies bindings, hashes and strict boolean/integer fields. It cannot authenticate a fabricated local receipt. Wrapper exit 0 is never a child exit. Missing artifacts, drift, failed child or nonterminal results never COMPLETE. FAILED/UNKNOWN retains its writer/worker lease unless terminal quiescence is explicitly evidenced. COMPLETE requires verified terminal quiescence and releases the lease and `prepare` returns REUSE after hashing retained artifacts again. Completed task IDs cannot be overwritten. If completed artifacts or receipts drift, retain the immutable historical task and submit a new authorized replacement task identity and replacement dependent tasks; completed-state rewriting and reconciliation are deliberately unsupported.

After any interruption call `recover`. A prior ISSUING becomes UNKNOWN, retaining its lease. No subsequent issue/recover/status returns the old request. Pending prepare/issue calls refuse until explicit recovery; after recovery, an unrelated observation does not suppress a newly guarded issue envelope. A crash before the tool call can sacrifice delivery to avoid duplicate dispatch; reconcile actual native records before abandonment. There is no exactly-once transport claim. No automatic retry uses a new dispatch ID. A genuinely new task needs an explicit new task ID after reconciliation. Locks coordinate this CLI, not direct external writers.

Dependencies must all COMPLETE with unchanged artifacts. Unknown/missing/failed prerequisites block with their exact IDs. Unobserved/busy workers block. Original writer claims are retained and resolved again under the DB lock before prepare/issue. A changed claim identity blocks that task; held leases conservatively cover both the original normalized paths and current resolved claims. Filesystem changes outside this lock remain outside its enforcement. Issuing consumes an IDLE observation by recording BUSY. Completion does not restore availability: the operator must submit a fresh native observation before later work can dispatch. Keep one live DB, its `.lock`, and required receipts/artifacts under the task work root. No SQLite pickle fallback or credential storage. Rollback: stop calling the CLI; preserve DB and referenced artifacts until superseded evidence is verified. Tests use isolated temporary DBs under the supplied pytest `--basetemp` work root.

Official implementation references: [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers). Dependencies and transitive versions are pinned by `uv.lock`; no API key is needed.

Run focused tests from the repository root with `uv run --locked --project tools/project-coordinator pytest -q tools/project-coordinator/tests --basetemp work/<run>/tests`. `--project` selects the environment; it does not change pytest discovery working directory.

Task objects accept exactly the documented input fields: internal dispatch/status/lease fields and unknown properties are rejected. Dependency IDs and paths must be nonempty strings; cycles are rejected before checkpointing. Status/recover remain compatible with existing V1 tasks without original-claim fields, using their retained normalized paths.


Worker replacement is a checkpointed `rebind` event, never a manual dictionary edit. It rejects pending checkpoints until explicit recovery/reconciliation, checks project/root/role/old/new identity, and leaves the replacement UNKNOWN until a fresh observation. Every prepared dispatch pins `dispatch_thread_id`; the same atomic rebind freezes all legacy dispatch identities on that role before changing its current mapping. Historical completion/receipt files remain intact, replay exposes no native request, and other held dispatches remain unchanged. Retain the exact event ID and evidence reference for audit. A new task ID is required to replace failed work; released old dispatches never resend.

`summary` uses a query-only read transaction on the existing database. It neither creates an absent database/lock nor invokes/resumes the graph, reconciles dispatches or sends native requests. It reports checkpoint identity and `recovery_required` for pending checkpoints; `pending_checkpoint` and `uncertain_held_dispatches` distinguish pending graph execution from stored uncertain ownership. Explicitly recover/reconcile as appropriate. The returned project/workers/tasks keys remain, but task details are a compact allowlist (identity, state, ownership, dependencies, blockers and tracking refs). Worker evidence, prompts, events, receipts, native arguments and full history are omitted. Held tasks sort first, then task ID. `bounds` reports total tasks, next offset, string/list limits and every truncated field. Continue with the same limit and `next_offset`; a changing checkpoint ID means restart pagination for a coherent snapshot. Unreturned rows/fields are not proof of absence. Status/recover keep their earlier full responses and recovery side effects.

`summary` returns the roles stored in the bound project map. Its role count is derived from current bindings and stored observations; unexpected stored roles remain visible in full status and are reported as omitted in the bounded summary.

## Project-bound worker policy

`register` adds one worker only when `--project-policy` supplies a validated policy bound to the same project ID and canonical root. The policy has `schema_version`, `project_id`, `root`, `worker_registrations`, and `legacy_leaseless_beads` fields, with optional `project_report_registrations` for project-only reporting identities. Registration authorization is an exact role/thread pair; legacy authorization is an exact Beads-ID/assignee pair. No project-specific identities are compiled into the coordinator.

```json
{
  "schema_version": 1,
  "project_id": "P-example",
  "root": "/absolute/canonical/project",
  "worker_registrations": [{"worker": "researcher", "thread_id": "research-thread"}],
  "project_report_registrations": [{"role": "product_owner", "thread_id": "product-owner-thread"}, {"role": "scrum_master", "thread_id": "scrum-master-thread"}],
  "legacy_leaseless_beads": [{"beads_id": "global-example-legacy", "assignee": "codex:coordinator:task"}]
}
```

Pass this file with `--project-policy` on `register`; pass it on `project-report` for a separately configured project role, or on `prepare`/`issue` only when the project needs an explicitly authorized legacy lease exception. Project-report registrations are checked independently from the worker map and cannot reuse a task-worker or coordinator thread. A registered worker starts `UNKNOWN` and still needs a fresh observation before dispatch. Registration compares the full current role map, records an UNKNOWN observation and hashes the exact old-state projection; every existing task, observation, receipt, dispatch, lease and audit entry remains unchanged. Pending checkpoints still require explicit recovery before registration. The schema definition is available as `#/$defs/projectPolicy` in `schema.json`.

Retain the exact event and referenced evidence through later checkpoints. Do not restore an old database over newer state; use a separately reviewed compatible source and reconciliation path if recovery is needed.

## Dependency refresh and retained checkpoint recovery

This candidate pins `langgraph==1.2.16` and `langgraph-checkpoint-sqlite==3.1.2`.
The retained old lock uses 1.2.2/3.1.0. The only resolved transitive changes are
`langgraph-sdk` 0.3.15 → 0.4.8 and `websockets` 17.2 → 16.1.1. Both isolated
locked environments use Python 3.14.7; the canonical environment is unchanged.

Saver 3.1.2 adds `writes.task_path` during its normal write-capable setup.
Summary skips setup to preserve its query-only contract, so it detects that
column with `PRAGMA table_info(writes)` and selects the saver's matching read
path. Reading an old schema does not create the column. The owned restore
roundtrip permits the dependency's ordinary additive setup; it does not prune
checkpoints or run a custom migration. This compatibility branch uses the
pinned saver's internal `_has_task_path` flag and must be rechecked when that
pin changes. Upstream implementation: [SqliteSaver](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py).

The reproducible run lives under
`<run-root>/`: `run_dependency_compat.py`
uses explicit old/new `.venv/bin/python` paths and
`candidate/project-coordinator/tests/test_dependency_compat.py`. Its evidence
is `validation/RESULT.json`, numbered old/new checkpoint projections and
`validation/targeted-tests.txt`. The immutable Root snapshot's manifest,
0600 mode, byte count and SHA-256 must match `SNAPSHOT-INPUT.json` before use.
Use only one private `validation/working.sqlite` restore sequentially. The
runner requires that working restore to match the immutable input at start;
inspect an already advanced restore with the probe rather than overwriting it.

The matrix reads every retained checkpoint in both environments, compares full
model/component digests, history and original SQL row digests, writes/recoveries
with the new environment, reads/recovers those checkpoints with the old one,
and rereads old-written checkpoints with the new one. Pending registration and
pending issue recovery use explicitly synthetic small fixtures, including held
and uncertain work, guard claim retention, replay and no resend. Focused public
CLI/API checks cover missing/expired guard, prepared actor transfer and renewal,
registration preservation/replay, UNKNOWN newly registered roles and query-only summary. No test
executes a returned native envelope or modifies the external manual claims.

Retain the old source/lock/environment, compatible new source/lock/environment,
Root's one immutable rollback input, the owned working restore and evidence
through Root's adoption and rollback decision. Do not restore an older snapshot
over newer canonical work. Fixture compatibility and the owned snapshot
roundtrip do not accept canonical adoption; Root alone owns that final caller
verification and any live source/environment/database change.

After the writer freezes, independently inspect the same advanced working copy
in each locked environment without copying or resetting it (from repository root):

```bash
run_root="<run-root>"
for source in old-source candidate; do
  "$run_root/$source/project-coordinator/.venv/bin/python" \
    "$run_root/candidate/project-coordinator/tests/test_dependency_compat.py" \
    inspect "$run_root/validation/working.sqlite"
done
```

Compare the returned model/components, checkpoint, pending state, full history,
retained history, original SQL row hashes and query-only summary digest against
`validation/06-new-read-old-checkpoints.json`. The inspector hashes the main database and nonempty WAL
before/after and refuses an unexpected write; SQLite may create empty WAL/shared
memory sidecars while reading. Each subprocess exits before the
next starts. For a small independent regression check, run the candidate's
`tests/test_dependency_compat.py` and
`tests/test_beads.py::test_prepared_claim_rejects_new_actor_context_and_allows_same_actor_renewal`
with its explicit `.venv/bin/python -m pytest`, using a new small pytest
`--basetemp` under this run directory. It creates only synthetic fixtures.
