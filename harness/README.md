# Caller-owned evidence harness

These tools validate retained allocation, build, review and runtime evidence. They
never allocate, dispatch, publish, deploy or complete issues. Passing parser fixtures
proves harness behavior; it does not prove a live workflow or product acceptance.

Every admission/acceptance API requires an explicit validated policy argument.
Every operational CLI requires `--policy /absolute/path/to/project-policy.json`.
The `--identity` diagnostics and `candidate_identity.py` only report source identity.
There is no project policy default or repository search fallback.

The caller owns this policy; save a project-specific copy outside the generic harness:

```json
{
  "project_id": "sample-project",
  "worker_roles": ["builder", "reviewer"],
  "linear_workspace": "sample-team",
  "acceptance_issue": "APP-42",
  "workflows": {
    "implementation": {"workflow": "sample-build", "node": "build-result"},
    "review": {"workflow": "sample-review", "node": "review-result"},
    "runtime_suite": {"workflow": "sample-runtime-suite", "node": "suite-result"},
    "wrong_identity": {"workflow": "sample-runtime", "node": "runtime-result"}
  }
}
```

The exact fields and four workflow purposes are required. An optional boolean
`require_writer_paths` rejects an empty writer scope when true; it defaults to false
for callers that admit read-only work. Values must be nonempty
canonical strings; worker roles must be unique and match
`[a-z][a-z0-9_-]{0,63}`; the workspace must be a URL segment and the issue an
uppercase key plus numeric suffix. Workflow names must be distinct. The neutral
names above are examples: choose the actual workflow and selected return node for
each purpose. Those workflows must retain the structured native evidence contract;
the policy changes identity bindings, not acceptance semantics.

```bash
python harness/linear_intake.py RELEASE.json SUMMARY.json --policy POLICY.json
python harness/post_builder.py RELEASE.json BUILDER.json SUMMARY.json --expected-run-id RUN_ID --policy POLICY.json
python harness/prepare_runtime.py OUTPUT_VIEW.json SUMMARY.json CONTROLS_DIR --policy POLICY.json
python harness/acceptance.py BUNDLE.json --policy POLICY.json
```

The equivalent Python signatures are `intake(release, summary, policy)`,
`output_view(initial_path, builder_path, summary_path, expected_run_id, policy)`,
`check_view(view, candidate, policy)`, `prepare(root, release_path, summary_path,
output, policy)` and `accept(bundle, directory, policy, failures=None)`.

Runtime preparation copies the validated policy into the new controls directory,
passes that exact file to baseline and deliberate-fault commands, and hashes it
alongside every other required scenario input. The executable identity includes
`project_policy.py`; changing validation changes the calibrated target identity.
The baseline must verify, the ownership-bypass control must fail its calibrated
assertion, and the wrong requested identity must be refused as inconclusive.
Existing source pins, allocation ownership, clean exact review head, certified
review report and frozen runtime dependency checks remain required.

Run all harness tests with the caller-supplied pinned Archon checkout:

```bash
ARCHON_SOURCE=/absolute/path/to/pinned-archon PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s harness -v
```

Native conformance tests verify the source revision against
`template/factory/pack.json` and each consumed source hash. They require that source;
no installed project settings or credentials are read.
