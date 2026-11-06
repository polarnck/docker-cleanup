# docker_cleanup_lib

Implementation package for `scripts/docker_cleanup.py`. The entry point is deliberately thin; all logic lives here.

## Flow

```text
doctor -> audit -> questions -> plan -> apply -> verify
```

1. `doctor` checks the Docker CLI, daemon, context, disk and warnings.
2. `audit` collects a read-only snapshot and classifies every object.
3. The agent asks for depth, age and volume-preview consent (see `SKILL.md`).
4. `plan` turns the answers into an exact list of objects and commands.
5. `apply` re-validates the live state and removes only approved objects.
6. `verify` is part of the apply output: disk before/after, log and restore hints.

## Modules

| File | Responsibility |
|---|---|
| `constants.py` | Schema version, exit codes, verdict labels, reason strings, UI messages. |
| `helpers.py` | Size/time parsing, Markdown helpers, state directory, inspect helpers, ID lookup. |
| `docker_cli.py` | Docker CLI wrapper, daemon/context checks, `doctor` data and output. |
| `collect.py` | Read-only snapshot: containers, images, volumes, networks, build cache, compose projects, bind mounts. |
| `registry.py` | Registry digest checks for unused images. |
| `peek.py` | Optional read-only volume preview (`--peek`, `--peek-volume`). |
| `classify.py` | Verdicts (`safe`, `cost`, `decision`, `protected`), items, depth selection, audit payload. |
| `plan.py` | Plan construction, plan loading, summary refresh. |
| `render.py` | Markdown renderers for the audit and the plan. |
| `apply.py` | Apply-time re-validation, removal, dry-run, logging and restore hints. |
| `cli.py` | Argument parser, command handlers and `main`. |

## Dependency direction

Lower-level modules do not import `cli` or `apply`.

```text
constants
helpers
docker_cli
collect   registry   peek
classify
plan
render
apply
cli
```

## Data flow

1. `collect` reads Docker and normalizes the data.
2. `classify` assigns a verdict and a reason code to each object.
3. `plan` selects candidates for the chosen depth and age.
4. `render` presents the audit and the exact plan.
5. `apply` re-checks context, daemon, object ID, state, usage and cutoff before every removal.
6. `apply` writes a JSON + Markdown log and reports real disk delta.

## Invariants

- `doctor`, `audit`, `plan`, `select` are read-only.
- `apply` is the only module allowed to call destructive Docker commands.
- No `docker system prune`, `docker rm -v`, `docker rmi -f`, `docker volume prune -a` or `sudo`.
- Running containers, in-use volumes, built-in/swarm networks and used images are protected.
- Decision items need `--allow-decision`, `approved: true` and `confirm == name`.
- The plan is user-editable; apply merges the live verdict conservatively and never downgrades a decision item.
- Every Docker call has a timeout.
- Tests live outside the published skill.

## Adding code

- Pure parsing/formatting goes to `helpers.py`.
- Docker reads go to `collect.py`, `registry.py` or `peek.py`.
- Verdict and depth logic goes to `classify.py`.
- Plan shape goes to `plan.py`.
- Markdown goes to `render.py`.
- Destructive logic goes only to `apply.py`.
- CLI wiring goes to `cli.py`.
- Keep the entry point under 50 lines and keep modules below roughly 700 lines.
