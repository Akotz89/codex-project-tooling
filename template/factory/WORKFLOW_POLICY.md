# Factory workflow policy

This project uses Archon's shared SDLC workflows. The requirements below are
project guidance for those workflows; they do not replace workflow gates or grant
permission to publish, comment, merge, deploy, or change schedules.

## Native shared execution

Read `.factory/project-context.md` and this project's
`_bmad/custom/ticketing-store-config.toml` before a selected native workflow.
The exact local Linear project remains the accepted-work authority. The native
Core coordinator retains claims, worker liveness, writer ownership and dispatch;
Archon owns the selected bounded delivery/review workflow. Installation and
readiness do not select backlog work or authorize a scheduler. Use one accepted
implementation/review route and preserve the original caller's acceptance.

Worker choices are limited to `gpt-6-luna` with currently supported effort and
`gpt-6.1-sol` with low, medium or high effort. Preserve an explicit permitted pair;
refuse a forbidden pair. Optional Jev assessment failure permits only an already
validated ordinary pair. Current coordinator settings remain unchanged. Usage
collection is explicit and project-local; no collector daemon is installed.

For new project-bound native subagent preparations, `.factory/consumer.json` may
set `jev_router.allowed_models` to a nonempty unique list from the shared worker
policy, for example `"allowed_models": ["gpt-6-luna"]`. The receipt retains the
capability identity and complete shared-policy pair set; Jev's Choice includes
only currently supported pairs whose model is allowed. The hook rechecks the
prepared policy against the current project setting before launch. With the setting absent, the shared model
policy applies unchanged. Existing workers and coordinator settings are unaffected.

## Scope and backlog bootstrap

Read `MISSION.md` in full. Its product scope, exclusions, hard invariants, and
human-owned decisions govern every backlog and delivery decision. Leave work that
contradicts them out of a proposed backlog and report the conflict.

For a product-document bootstrap, make the first ticket the smallest real core path
that can be run and verified end to end. The ticket must name an exact start-command
argv with a port placeholder, health path, build-identity path, persistent-state
path or environment variable, test command, and pull-request CI check. HTTP apps
report `FACTORY_RUNTIME_CANDIDATE` exactly at the build-identity path when it is set,
and otherwise report their Git commit. Reuse the project's declared ordinary gate;
adding tests or CI is allowed, but weakening its judge is not. In an existing
codebase that already provides this foundation, the first ticket is an ordinary
first story.

## Factory-owned workflow inputs

Factory supplies this state-label mapping to `archon-triage`, `archon-ship`, and
`archon-lifecycle` unless the caller explicitly supplies `state_labels`, including
an explicit `{}`:

| State | GitHub label |
|---|---|
| `READY` | `archon-ready` |
| `DESIGN_FIRST` | `archon-design-first` |
| `NEEDS_CONTRACT_WORK` | `archon-needs-contract` |
| `BLOCKED` | `archon-blocked` |
| `NO_ACTION` | `archon-close` |

Automatic lifecycle intake requires the complete mapping and `publish=true` to
mark selected issues as touched. Publication remains off by default. Hold-comment
publication separately requires `publish_holds=true` in approve or auto merge mode;
`merge_mode=preview` is always read-only, and a published hold never authorizes a
merge.

## Runtime and review evidence

Runtime and holdout evidence must exercise the delivered candidate and match its
current source identity. When using the factory runtime host, follow
`factory/RUNTIME_HOST.md`: prepare the resource from the delivering checkout at its
expected revision, require the configured health and build-identity checks, use
fresh state for each attempt, and preserve the typed result as evidence. Ordinary
checks do not substitute for required runtime or holdout evidence.

Independent review evidence may be Archon's canonical current-head report, a
genuine review by an independent human, or an external independent report format
that project guidance accepts. In every case it must identify the PR's current head,
be credible and independent of the implementer, state a ready verdict, and contain
no unresolved blocking findings. Stale, ambiguous, or self-attested evidence holds.

## BMAD Build manager-to-worker handoff

These manager duties apply only when the current native handoff assigns the chat the manager/orchestrator role. For new work and continuations, the assigned manager owns accepted scope, claims, writer reconciliation, dispatch, coordination metadata, read-only inspection and verification, review, and original-caller acceptance. Source and test edits, Git staging/commit/push, and task-owned cleanup execution belong in the bounded worker assignment; the manager coordinates those actions and inspects their results read-only.

The assigned execution worker performs its bounded work directly. Reading manager guidance is not a reason to redispatch. Before an assigned manager makes implementation or task-owned execution writes, it dispatches the actual bounded worker through the existing native route. The plan and typed request carry the complete accepted scope, task and source boundaries, writer ownership, project context, exclusions, caller acceptance, and exact artifact paths.

Preserve the selected delivery route and exact launch-ready assessed worker pair; do not change the running Core model. Existing workers and same-task continuations keep their current settings. This role guidance adds no tool-denial rule and makes no universal-enforcement claim.
