# Working in this repo

A CLI that runs LLM solvers against containerized SWE-bench-style coding tasks and grades the
resulting patch. See `README.md` for usage, `ARCHITECTURE.md` for structure, `DESIGN_NOTES.md`
for why the design is what it is — read that one before proposing a change to the harness
itself, since most obvious alternatives are already there with the reason they were rejected.

## Setup

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e ".[gemini]"
```

Requires a running Docker daemon. `GEMINI_API_KEY` goes in `.env` (gitignored) — an exported
shell variable silently overrides it, which the CLI warns about.

## Tests

```bash
python tests/unit_checks.py     # 28 checks, plain python, no pytest, no Docker, no network
```

Run this after any change to `agent_evals/`. It is fast and covers diff repair, prompt
construction and ranking, protected-path matching, JUnit parsing and the `task.json` contract.
`tests/linux/` runs the same suite in a container to catch path and line-ending assumptions.

For anything touching the container lifecycle, also run the real thing on the toy bundle —
it builds in ~30s and needs no dataset download:

```bash
evals init bundles/toy-calc-001 && evals validate bundles/toy-calc-001
evals run bundles/toy-calc-001 --solver stub      # must be UNSOLVED
evals run bundles/toy-calc-001 --solver oracle    # must be SOLVED
```

Those two verdicts are the harness's own smoke test: `stub` changes nothing, `oracle` applies
the known-good patch. If either flips, the harness is broken, not the model.

## Module map

| File | Holds |
|---|---|
| `cli.py` | Typer app, 11 commands, argument validation |
| `runner.py` | The orchestration — build, solve, grade, verdict. Largest and most load-bearing |
| `solvers.py` | Provider clients, prompt construction, file ranking, LLM diff repair |
| `bundle.py` | `TaskBundle`, `task.json` parsing and validation |
| `docker_mgr.py` | Subprocess wrapper over the `docker` CLI |
| `db.py` | SQLite run log; every run is a row, replayable by id |
| `scaffold.py` | SWE-bench Pro dataset row → bundle directory |

## Invariants — don't break these

1. **The solver never sees `pass2pass`/`fail2pass`.** They are deleted from the image at build
   time, not hidden at runtime. Any change that makes them reachable from the solver's
   container invalidates every result the tool produces.
2. **Grading happens in a fresh container**, never the one the solver worked in.
3. **`protected_paths` are reset from git before grading**, so a patch that edits test
   infrastructure cannot influence its own grade.
4. **Bundle files are `eol=lf`** (see `.gitattributes`). CRLF breaks `git apply` inside the
   container with a confusing "corrupt patch" error.

## Conventions

- Commit messages describe the behaviour change, in the imperative, not the file list.
- Docs justify design choices on their merits, with the concrete failure they prevent.
- No new runtime dependencies without a reason; providers stay optional extras.
