# Upgrading the factory

Upgrade an installed factory from a separate, reviewed successor checkout. Run
`python <successor>/bin/factory.py init` from each adopting application repository.
Keep the previous source checkout unchanged and available for projects that still
bind to it and for rollback, together with each project's latest preimages in shared
state outside the repository. The default is `$XDG_STATE_HOME/codex-project-tooling`
or `~/.local/state/codex-project-tooling` on Linux,
`%LOCALAPPDATA%\codex-project-tooling` on Windows, or
`~/Library/Application Support/codex-project-tooling` on macOS. Set
`CODEX_PROJECT_TOOLING_STATE_HOME` to select another shared state root; project
paths are hashed to keep backups separate. Existing project-local `.factory/backup`
files are left untouched.

- `init` installs the files in `template/` and the Archon source pinned in
  `factory/pack.json`, then validates that source.
- Files you write for your project are never overwritten: `MISSION.md`,
  `harness/END-TO-END.md`, `harness/harness.config.json`,
  `.factory/holdout/HOLDOUT.md`, and the other project files listed in the installer.
- An installed factory file you changed is copied to its fixed external shared-state
  path before the new version replaces it. Each path keeps only the immediate
  preimage; a later change replaces that backup atomically instead of adding a
  numbered copy. This supports one-step rollback.
- The generic workflow policy stays in the pinned shared source checkout. On
  upgrade, an existing byte-identical project copy is backed up once and removed;
  a locally modified copy and its engineering.md reference are preserved.
- Run `python factory/consumer.py doctor` afterwards. It checks the pinned source
  and that every shared workflow validates.

What the factory owns, and what Archon owns:

- **Archon** owns every workflow and agent: triage, delivery, review,
  validation, runtime and holdout verification, repair, discoveries, merging,
  deployment, and scheduling through native triggers.
- **The factory** owns your project's context files, the installation, and the
  thin consumer that invokes Archon and shows its state.
- **Runtime scenarios** are project inputs, and must exercise fresh environments
  running the actual candidate.

If you scheduled the factory, rerun `python factory/consumer.py schedule install`
after an upgrade, so the trigger binding picks up any new inputs.

## Shared Python adapters

New installations retain the existing script and import paths as generated
adapters. The factory source checkout is required at runtime: its exact Git
revision and Python/asset digest are bound at installation. Root, local Linear
address and project context must match too. Configure those project artifacts
before installation; scaffolding without them cannot run the adapters.

Upgrade from a reviewed, independently pinned successor source, then rerun its
official installer in each adopting project. Changed managed files are backed
up in shared state outside the repository; personal files and native configuration
are preserved.
Keep the previous source and latest preimages for one-step rollback. Do not edit
generated adapters or repoint an existing immutable source checkout. Frozen
installations continue using their original implementation until explicitly adopted.

CI, application helpers, mutation defaults and usage collection use the verified
adopting root. Child runtime and routing processes relaunch the local adapter.
The local pack and assessment JSON files are compatibility provenance; the bound
source owns the assets used by shared code. Source-tree adapters under `template/`
are development entry points, not portable installed files.
