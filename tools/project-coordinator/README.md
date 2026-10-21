# Persistent project coordinator

Local LangGraph StateGraph (`transition → readiness`) with synchronous SqliteSaver checkpoints and one exclusive flock per database. Linear owns plan/scope/dependencies; these inputs are observations from the coordinator. Beads references are mandatory. No daemon, native app endpoint, LLM, or provider calls.

Run `uv sync --locked --project tools/project-coordinator` then `scripts/project-coordinator --db work/<run>/state/coordinator.sqlite COMMAND --input EVENT.json`. Every command outputs compact JSON; errors exit 2. Import `project_coordinator.apply_event(db,event)` with the same contract plus `op`.

## Exact event contract

Every mutating command requires a unique `event_id` string. Identical event IDs and canonical payloads return REPLAY; altered payloads with the same ID are rejected. A replay never exposes native arguments again. Status/summary/recover need no event ID.

`init`: `{ "event_id":"init-1", "project": { "project_id": "example-project", "root": "/absolute/canonical/project", "linear_ref": "https://linear.app/...", "beads_ref": "global:execution-id", "coordinator_thread_id": "actual-root-thread", "workers": {"implementer":"implementation-thread", "reviewer":"review-thread"} } }`

All other commands require common binding `{ "project_id":"example-project", "root":"/absolute/canonical/project" }` plus:

| Command | Additional fields |
| --- | --- |
| add | `task:{id,linear_ref,beads_ref,worker,prompt,source_digest,scope_digest,dependencies:[],writer_paths:[]}`; paths project-relative or absolute within root; empty list means read-only |
| observe | `worker`, `state:"IDLE" / "BUSY" / "UNKNOWN"`, `evidence:"source observation reference"` |
| prepare | `task_id`, `source_digest`, `scope_digest` matching immutable task identity |
| issue | `task_id`, `dispatch_id` returned by prepare |
| ack | `task_id`, `dispatch_id`, `receipt:{path,sha256}` pointing at actual native MCP acknowledgement JSON |
| result | `task_id`, `dispatch_id`, `receipt:{path,sha256}` pointing at normalized result below |
| reconcile | `task_id`, `dispatch_id`, `quiescent:true`, `evidence:"observed stopped/no pending send reference"`; abandons PREPARED/UNKNOWN/ACKNOWLEDGED/FAILED work as FAILED and releases lease; late results for a released dispatch are rejected |
| rebind | `worker`, `expected_old_thread_id`, `new_thread_id`, `evidence`; compare-and-swap replacement, requiring no held/uncertain dispatch and no observed BUSY worker |
| summary | common binding; optional `offset` (default0), `limit` (default50, max100) |
| status / recover | common binding only; existing full response/recovery semantics |

Every input is a JSON object, not a JSON string. CLI command supplies `op`. Inputs and receipts must contain no credentials or full logs.

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

After any interruption call `recover`. A prior ISSUING becomes UNKNOWN, retaining its lease. No subsequent issue/recover/status returns the old request. Resuming an unrelated pending observation does not suppress a new issue envelope. A crash before the tool call can sacrifice delivery to avoid duplicate dispatch; reconcile actual native records before abandonment. There is no exactly-once transport claim. No automatic retry uses a new dispatch ID. A genuinely new task needs an explicit new task ID after reconciliation. Locks coordinate this CLI, not direct external writers.

Dependencies must all COMPLETE with unchanged artifacts. Unknown/missing/failed prerequisites block with their exact IDs. Unobserved/busy workers block. Original writer claims are retained and resolved again under the DB lock before prepare/issue. A changed claim identity blocks that task; held leases conservatively cover both the original normalized paths and current resolved claims. Filesystem changes outside this lock remain outside its enforcement. Issuing consumes an IDLE observation by recording BUSY. Completion does not restore availability: the operator must submit a fresh native observation before later work can dispatch. Keep one live DB, its `.lock`, and required receipts/artifacts under the task work root. No SQLite pickle fallback or credential storage. Rollback: stop calling the CLI; preserve DB and referenced artifacts until superseded evidence is verified. Tests use isolated temporary DBs under the supplied pytest `--basetemp` work root.

Official implementation references: [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers). Dependencies and transitive versions are pinned by `uv.lock`; no API key is needed.

Run focused tests from the repository root with `uv run --locked --project tools/project-coordinator pytest -q tools/project-coordinator/tests --basetemp work/<run>/tests`. `--project` selects the environment; it does not change pytest discovery working directory.

Task objects accept exactly the documented input fields: internal dispatch/status/lease fields and unknown properties are rejected. Dependency IDs and paths must be nonempty strings; cycles are rejected before checkpointing. Status/recover remain compatible with existing V1 tasks without original-claim fields, using their retained normalized paths.


Worker replacement is a checkpointed `rebind` event, never a manual dictionary edit. It rejects pending checkpoints until explicit recovery/reconciliation, checks project/root/role/old/new identity, and leaves the replacement UNKNOWN until a fresh observation. Every prepared dispatch pins `dispatch_thread_id`; the same atomic rebind freezes all legacy dispatch identities on that role before changing its current mapping. Historical completion/receipt files remain intact, replay exposes no native request, and other held dispatches remain unchanged. Retain the exact event ID and evidence reference for audit. A new task ID is required to replace failed work; released old dispatches never resend.

`summary` uses a query-only read transaction on the existing database. It neither creates an absent database/lock nor invokes/resumes the graph, reconciles dispatches or sends native requests. It reports checkpoint identity and `recovery_required` for pending checkpoints; `pending_checkpoint` and `uncertain_held_dispatches` distinguish pending graph execution from stored uncertain ownership. Explicitly recover/reconcile as appropriate. The returned project/workers/tasks keys remain, but task details are a compact allowlist (identity, state, ownership, dependencies, blockers and tracking refs). Worker evidence, prompts, events, receipts, native arguments and full history are omitted. Held tasks sort first, then task ID. `bounds` reports total tasks, next offset, string/list limits and every truncated field. Continue with the same limit and `next_offset`; a changing checkpoint ID means restart pagination for a coherent snapshot. Unreturned rows/fields are not proof of absence. Status/recover keep their earlier full responses and recovery side effects.

Worker roles come from the nonempty project `workers` map. Names match `[a-z][a-z0-9_-]{0,63}`; every worker thread and the coordinator thread must be distinct. Tasks, observations and replacements must name a bound role. Summary includes every safe project role in lexical order. Invalid or unbound stored roles are omitted with total/omitted counts and a fixed truncation diagnostic; full status retains them for investigation. Output bounds do not bound the memory needed to deserialize the latest checkpoint or the work needed to evaluate readiness.
