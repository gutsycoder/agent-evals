# Agent Evals

A CLI for running LLM solvers against containerized SWE-bench-style coding tasks.

You describe a task as a **bundle** (a repo + commit, a problem statement, a golden patch,
and two buckets of tests). The CLI builds a Docker image of that repo, proves the task is
well-formed, asks a solver to fix the bug without ever showing it the grading tests, then
grades the result in a fresh container and writes a structured report.

I built this to understand what actually makes an agent benchmark trustworthy. The headline
number in a SWE-bench-style eval is easy to produce and easy to get wrong — a leaked test, a
patch that edits `conftest.py`, a task whose fail2pass test already passes, and the score
means nothing. So the design here is mostly about the guarantees around the number:
the graded tests are physically absent from the solver's container, the solver's patch is
graded in a fresh one, test infrastructure it touched is reset from git before grading, and
every run is replayable from a SQLite row. [`DESIGN_NOTES.md`](DESIGN_NOTES.md) is the
honest version — what broke, what it cost to find, and what I'd build next.

```
evals init      bundles/my-task          # build a Docker image of the repo @ commit
evals validate  bundles/my-task          # prove the task is well-formed (baseline guardrails)
evals run       bundles/my-task --solver llm    # solve → grade → verdict + JSON report
evals logs      42                       # what happened in run 42
```

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [**Worked example — copy, paste, done**](#worked-example--copy-paste-done)
- [Verified results](#verified-results)
- [Reproduce everything from scratch](#reproduce-everything-from-scratch)
- [The bundle format](#the-bundle-format)
- [`task.json` reference](#taskjson-reference)
- [Command reference](#command-reference)
- [Verdicts](#verdicts)
- [Configuring an LLM provider](#configuring-an-llm-provider)
- [Generating bundles from SWE-bench Pro](#generating-bundles-from-swe-bench-pro)
- [Querying the run database](#querying-the-run-database)
- [Artifacts](#artifacts)
- [Supporting other languages](#supporting-other-languages)
- [Design tradeoffs](#design-tradeoffs)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

### Further documentation

| Document | Answers |
|---|---|
| `README.md` (this file) | How do I use it? |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How is it built? HLD, LLD, database schema, sequence and flow diagrams. |
| [`DESIGN_NOTES.md`](DESIGN_NOTES.md) | *Why* is it built this way — the trade-offs, the alternatives rejected, the failures, and what they cost to find. |

---

## Install

**Requirements:** Python 3.11+ and a running Docker daemon. Nothing else — no language
runtimes for the repos under test; those live in containers.

### 1. Install Docker

| Platform | How |
|---|---|
| **macOS** | [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/), or `brew install --cask docker`. Launch it once so the daemon starts. |
| **Ubuntu / Debian** | `sudo apt-get update && sudo apt-get install -y docker.io`, then `sudo usermod -aG docker $USER` and log out/in so you can run docker without sudo. |
| **Windows** | [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/) with the WSL 2 backend. |

On Apple Silicon the SWE-bench Pro images are `linux/amd64`, so Docker Desktop must have
**Rosetta / "Use Virtualization framework"** enabled. They run under emulation and are
noticeably slower — the toy bundle is unaffected.

### 2. Install the CLI

<details open>
<summary><b>macOS / Linux</b></summary>

```bash
git clone https://github.com/gutsycoder/agent-evals.git && cd agent-evals
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[gemini]"
```
</details>

<details>
<summary><b>Windows — PowerShell</b></summary>

```powershell
git clone https://github.com/gutsycoder/agent-evals.git; cd agent-evals
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[gemini]"
```
</details>

Optional extras: `.[anthropic]`, `.[openai]`, `.[bedrock]`, `.[all]`, and `.[scaffold]`
(only needed for `evals scaffold --instance-id`, which fetches a dataset row live).

### 3. Get a Gemini API key

**Gemini is the default provider and has a free tier**, so this is the only credential you
need to run everything in this README.

1. Go to **<https://aistudio.google.com/apikey>**
2. Sign in with a Google account → **Create API key** → copy it (starts with `AIza…`)

Then put it in a `.env` file in the repo root — this is the recommended way, because it
survives shell restarts and works identically on all three platforms:

```bash
cp .env.example .env
```

Open `.env` and set the one line:

```
GEMINI_API_KEY=AIza...your-key-here
```

`.env` is gitignored and must never be committed. It's loaded automatically on every `evals`
invocation.

> **Gotcha worth knowing:** an *exported* shell variable always beats `.env`. If you
> previously ran `export GEMINI_API_KEY=...` (or `$env:GEMINI_API_KEY=...`), that stale
> value wins and `.env` is ignored. The CLI prints a warning when it detects this, and tells
> you how to clear it: `unset GEMINI_API_KEY` (bash/zsh) or
> `Remove-Item Env:\GEMINI_API_KEY` (PowerShell).

### 4. Verify

```bash
docker info                          # daemon must be reachable
evals --help                          # 11 commands
evals providers --provider gemini     # one tiny API call — confirms the key works
```

Expected: `gemini     OK     model=gemini-3.1-flash-lite reply='OK'`

---

## Quick start

Three example bundles ship with the repo. `toy-calc-001` is small and builds in ~30s.

```bash
evals init     bundles/toy-calc-001     # build the image (cached; --force to rebuild)
evals validate bundles/toy-calc-001     # baseline guardrails must hold

evals run bundles/toy-calc-001 --solver stub      # expect UNSOLVED (a no-op solver)
evals run bundles/toy-calc-001 --solver oracle    # expect SOLVED   (applies the golden patch)

evals history                            # every command ever run
evals logs 3                             # details of run 3
```

`bundles/ansible-vars-001` and `bundles/vuls-redhat-001` are real
[SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) instances —
`ansible/ansible` (Python, 15 pass2pass + 1 fail2pass, all in one source file) and
`future-architect/vuls` (**Go** — proving the harness isn't Python-only). Both take a few
minutes to build the first time (pulling the dataset's prebuilt image).

**Why run `stub` and `oracle` first?** They're the two ends of the range and they test the
*harness*, not a model. `stub` changes nothing, so it must report `UNSOLVED`. `oracle`
applies the bundle's own known-correct patch, so it must report `SOLVED`. If oracle isn't
`SOLVED`, the bundle or the harness is broken — find out before spending money on an LLM.

---

## Worked example — copy, paste, done

Every command below is complete: no `<placeholders>`, nothing to look up. Run them in order
from the repo root after `pip install -e ".[gemini]"` and setting `GEMINI_API_KEY` in `.env`.
Uses `toy-calc-001`, which needs no large download — **the whole sequence takes ~3 minutes.**

### 1. Build the image and prove the task is well-formed

```bash
evals init bundles/toy-calc-001
```
```
init: built image evals/toy-calc-001:6b57adb34e5ddc02699a823b7d06cff6ec122db3 (task_id=toy-calc-001 commit=6b57adb3...)
hidden_paths stripped from image: tests/test_add.py
```

```bash
evals validate bundles/toy-calc-001
```
```
Bucket     Test                                                        Outcome  Expected    OK
---------  ----------------------------------------------------------  -------  ----------  --
pass2pass  tests.test_add::test_add_positive_numbers                   passed   passed      OK
pass2pass  tests.test_add::test_add_negative_numbers                   passed   passed      OK
fail2pass  tests.test_divzero::test_divide_by_zero_raises_value_error  failed   not passed  OK

pass2pass: OK (2 test(s), expected passed)
fail2pass: OK (1 test(s), expected not passed)
```

**This is the baseline invariant.** pass2pass passes, fail2pass *fails* — the task is real.

### 2. The two control solvers — these test the harness, not a model

```bash
evals run bundles/toy-calc-001 --solver stub      # changes nothing
```
```
fail2pass  tests.test_divzero::test_divide_by_zero_raises_value_error  failed   passed  FAIL
Verdict: UNSOLVED
```

```bash
evals run bundles/toy-calc-001 --solver oracle    # applies the golden patch
```
```
fail2pass  tests.test_divzero::test_divide_by_zero_raises_value_error  passed   passed  OK
Verdict: SOLVED
```

> **`stub` → UNSOLVED and `oracle` → SOLVED is the single most important result in this
> repo.** It proves the harness distinguishes a fix from a non-fix with no LLM involved. If
> oracle isn't SOLVED, stop — the harness or the bundle is broken.

> **Note on exit codes:** `evals run` exits **1** for any verdict except SOLVED. The stub run
> "failing" is the correct outcome, not an error.

### 3. Now the LLM

```bash
evals run bundles/toy-calc-001 --solver llm --keep-artifacts
```
```
Verdict: SOLVED
LLM: provider=gemini model=gemini-3.1-flash-lite latency=16.8s prompt~1243tok response=223ch temp=0.0 thinking=medium

Wrote report to reports/toy-calc-001-run-llm.json  (run_id=297; see `evals logs 297`)
Artifacts in artifacts/run-297
```

### 4. Query the database — no id to look up

```bash
evals history --limit 10
evals logs
```

`evals logs` with no argument shows the most recent run that recorded one, and says which it
picked. Pass an explicit id (`evals logs 42`) when you want a specific run.

```
# run 297 (most recent with a log)
run: verdict=SOLVED solver=llm patch_applied=True
```

### 5. Re-grade a stored patch for free

```bash
evals replay
```
```
Using run 295 (most recent with a stored patch).
Verdict: SOLVED
Wrote report to reports/toy-calc-001-replay-308.json  (run_id=308, source_run_id=295)
```

**Zero API calls.** It re-grades the diff already in the ledger — which is also the only way
to re-examine an LLM run, since the model itself is not reproducible.

Narrow the automatic pick with `--bundle`, or name a run outright:

```bash
evals replay --bundle toy-calc      # latest stored patch for that bundle
evals replay 295                    # exactly that run
```

### 6. Score a patch from anywhere — no solver involved

```bash
evals grade bundles/toy-calc-001 --diff-file bundles/toy-calc-001/patch.diff
```
```
Verdict: SOLVED
```

### 7. Prove the LLM never saw the grading tests

The ledger records where each run's artifacts went, so this needs no id either:

```bash
python -c "
import sqlite3, pathlib
run_id, adir = sqlite3.connect('runs.db').execute(
    \"SELECT run_id, artifacts_dir FROM runs WHERE task_id LIKE '%toy-calc%'\"
    \" AND artifacts_dir IS NOT NULL ORDER BY run_id DESC LIMIT 1\").fetchone()
listing = pathlib.Path(adir, 'solver_prompt.txt').read_text(encoding='utf-8') \
    .split('## Repository files')[1].split('## File contents')[0]
print('run', run_id, '->', adir)
print('files shown to the model:', len([f for f in listing.split(',') if f.strip()]))
print('HIDDEN test present:', 'tests/test_add.py' in listing)
"
```
```
run 314 -> artifacts/run-314
files shown to the model: 8
HIDDEN test present: False
```

The graded test is absent from the model's view entirely. On the Go bundle the same check
shows **40 other test files visible** with the graded one missing — both halves of the
requirement: hide the answers, show everything else.

### 8. Now do it on a real SWE-bench Pro instance — starting from the dataset

The bundles in `bundles/` are committed for convenience, but **nothing about them is
special**: each was produced by `evals scaffold` from a dataset instance id. Below is the
genuine end-to-end path — dataset → bundle → image → verdict — with **nothing pre-existing**.

```bash
pip install -e ".[scaffold]"        # adds `datasets`; only needed for scaffolding
```

#### 8a. Python — `ansible/ansible` (~15–25 min, downloads ~1.6 GB on first run)

```bash
evals scaffold \
  --instance-id instance_ansible__ansible-0ea40e09d1b35bcb69ff4d9cecf3d0defa4b36e8-v30a923fb5c164d6cd18280c02422f75e611e8fb2 \
  --out bundles/ansible-fresh
```
```
scaffold: wrote bundle ansible-vars-001 to bundles/ansible-fresh
  (repo=https://github.com/ansible/ansible.git commit=f7234968d241
   f2p=1 p2p=15 hidden=test/units/utils/test_vars.py)

Next: evals init bundles/ansible-fresh && evals validate bundles/ansible-fresh
```

```bash
evals init     bundles/ansible-fresh
evals validate bundles/ansible-fresh
evals run      bundles/ansible-fresh --solver stub        # UNSOLVED
evals run      bundles/ansible-fresh --solver oracle      # SOLVED
evals run      bundles/ansible-fresh --solver llm --keep-artifacts   # SOLVED
```

#### 8b. Go — `future-architect/vuls` (~30–45 min, downloads ~6.2 GB on first run)

This is the one that proves the harness isn't Python-only. Use `--verbose` — `go test`
compiles before running and looks frozen for minutes otherwise.

```bash
evals scaffold \
  --instance-id instance_future-architect__vuls-2c84be80b65d022c262956cd26fc79d8bb2f7010 \
  --out bundles/vuls-fresh
```
```
scaffold: wrote bundle vuls-redhat-001 to bundles/vuls-fresh
  (repo=https://github.com/future-architect/vuls.git commit=4c598bb9726d
   f2p=3 p2p=5 hidden=scanner/redhatbase_test.go)

Next: evals init bundles/vuls-fresh && evals validate bundles/vuls-fresh
```

```bash
evals init     bundles/vuls-fresh --verbose
evals validate bundles/vuls-fresh --verbose
evals run      bundles/vuls-fresh --solver stub   --verbose          # UNSOLVED
evals run      bundles/vuls-fresh --solver oracle --verbose          # SOLVED  ← key result
evals run      bundles/vuls-fresh --solver llm --keep-artifacts --verbose
```

The LLM does **not** solve `vuls` — expect `UNSOLVED` or `PARTIAL` with **pass2pass 5/5
still green**. See [Verified results](#verified-results) for why that's the honest result to
report, and [`DESIGN_NOTES.md`](DESIGN_NOTES.md) §14 for the three-model failure analysis.

#### What `scaffold` just did

One HTTP fetch of the dataset row, then it writes the entire bundle:

| It writes | From the dataset field |
|---|---|
| `task.json` — repo, commit, and per-language `setup_cmd` / `deps_cmd` / `test_cmd` | `repo`, `base_commit`, `repo_language`, `dockerhub_tag` |
| `description.md` — problem statement + Requirements + Interface | `problem_statement`, `requirements`, `interface` |
| `patch.diff` — the golden patch (oracle only, never shown to the LLM) | `patch` |
| `tests/pass2pass/` + `_selected_tests.txt` | `pass_to_pass` |
| `tests/fail2pass/` + `_selected_tests.txt` | `fail_to_pass`, applied from `test_patch` |
| `hidden_paths` — exactly the guardrail files that already exist in the base repo | derived |
| `source_row.json` — the raw row, as an audit trail | — |

Browse other instances at the
[SWE-bench Pro dataset viewer](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) and
substitute any `instance_id`; the flow is identical. `--use-dataset-image` (the default) uses
the prebuilt per-instance image; `--build-image` builds from a plain language base image
instead, which is what a non-SWE-bench repo would use.

---

## Verified results

Every number below was produced by deleting all `evals/*` images and rebuilding from
scratch. The JSON artifacts in [`reports/`](reports/) are the output of exactly this run.

| Bundle | Language | Buckets | `validate` | `stub` | `oracle` | `llm` |
|---|---|---|---|---|---|---|
| `toy-calc-001` | Python (plain base image) | 2 p2p + 1 f2p | passed | UNSOLVED | SOLVED | **SOLVED** |
| `ansible-vars-001` | Python (SWE-bench Pro) | 15 p2p + 1 f2p | passed | UNSOLVED | SOLVED | **SOLVED** |
| `vuls-redhat-001` | **Go** (SWE-bench Pro) | 5 p2p + 3 f2p | passed | UNSOLVED | SOLVED | UNSOLVED¹ |

LLM runs: `gemini-3.1-flash-lite`, `temperature=0.0`, single attempt, no retry.

**The `stub` / `oracle` pair is the important result.** It proves the harness distinguishes
a fix from a non-fix with no model involved: a no-op patch must score UNSOLVED and the
golden patch must score SOLVED, on all three bundles, in two languages.

¹ `vuls-redhat-001` is a hard instance and the model does not solve it — **pass2pass stays
5/5 green**, so nothing regresses; the model's patch applies and compiles but gets two
semantic details wrong. Best observed across many runs is `PARTIAL`. This is a legitimate
result to report, not a defect: published pass@1 on SWE-bench Pro's public set is ~23–59%
even for frontier models. See [`DESIGN_NOTES.md`](DESIGN_NOTES.md) §14 for the three-model
failure analysis and why a single-shot solver hits a ceiling here.

---

## Reproduce everything from scratch

### Starting from the dataset

Already covered step by step in the [worked example](#8-now-do-it-on-a-real-swe-bench-pro-instance--starting-from-the-dataset)
— `evals scaffold --instance-id ...` for both the Python and the Go instance, with the exact
ids, what scaffold writes, and the full ladder on each. That is the genuine first-time path:
dataset row in, verdict out, nothing pre-existing.

The rest of this section is for re-running what is already committed.

### Re-running the committed bundles

This is the full clean-room sequence. It rebuilds every image and regenerates every report.

**Time:** ~10 min for `toy-calc-001`; the two SWE-bench Pro bundles pull multi-GB prebuilt
images on first run (~8 GB total), so budget 30–60 min on a first run. Subsequent runs are
seconds, because images are cached by `evals/<task_id>:<commit>`.

<details open>
<summary><b>macOS / Linux</b></summary>

```bash
# 0. clean slate — removes evals containers and images (base images stay cached)
evals clean --all

# 1. fast, no Docker
python tests/unit_checks.py            # expect: ALL UNIT CHECKS PASSED
evals providers --provider gemini       # expect: gemini OK

# 2. the ladder, on each bundle
for b in toy-calc-001 ansible-vars-001 vuls-redhat-001; do
  evals init     bundles/$b
  evals validate bundles/$b
  evals run      bundles/$b --solver stub                       # expect UNSOLVED
  evals run      bundles/$b --solver oracle                     # expect SOLVED
  evals run      bundles/$b --solver llm --keep-artifacts       # needs GEMINI_API_KEY
done

# 3. the database
evals history --limit 20
evals logs                               # most recent run with a log

# 4. commands beyond the required three
evals replay                             # re-grade the latest stored diff — free
evals grade bundles/toy-calc-001 --diff-file bundles/toy-calc-001/patch.diff
```
</details>

<details>
<summary><b>Windows — PowerShell</b></summary>

```powershell
evals clean --all

python tests\unit_checks.py
evals providers --provider gemini

foreach ($b in "toy-calc-001","ansible-vars-001","vuls-redhat-001") {
  evals init     bundles/$b
  evals validate bundles/$b
  evals run      bundles/$b --solver stub
  evals run      bundles/$b --solver oracle
  evals run      bundles/$b --solver llm --keep-artifacts
}

evals history --limit 20
evals logs

evals replay
evals grade bundles/toy-calc-001 --diff-file bundles/toy-calc-001/patch.diff
```
</details>

> **Exit codes are meaningful.** `evals run` exits **1** for any verdict other than `SOLVED`,
> and `evals validate` exits 1 if the baseline invariant fails. That's deliberate (CI-friendly)
> — a `stub` run "failing" with exit 1 is the *correct* outcome, not a crash. In a `bash`
> loop with `set -e`, guard with `|| true`.

**Prove the hidden-test guarantee yourself** — this is the one property the whole harness
rests on, so don't take my word for it:

```bash
evals run bundles/vuls-redhat-001 --solver llm --keep-artifacts
python - <<'EOF'
import glob, pathlib
p = sorted(glob.glob('artifacts/run-*/solver_prompt.txt'))[-1]
listing = pathlib.Path(p).read_text(encoding='utf-8').split('## Repository files')[1].split('## File contents')[0]
files = [f.strip() for f in listing.split(',')]
print('files shown to the model :', len(files))
print('test files VISIBLE       :', len([f for f in files if '_test.go' in f]))
print('HIDDEN test present      :', 'scanner/redhatbase_test.go' in listing)
EOF
```

Expect `HIDDEN test present: False` with ~40 other test files visible — both halves of the
requirement: the graded tests are invisible, every other test is not.

---

## The bundle format

```
my-task/
  task.json            metadata + the four execution knobs
  description.md       the problem statement the solver reads
  patch.diff           the golden patch (powers the `oracle` solver)
  tests/
    pass2pass/
      path/to/test_file.py       <- mirrors the file's REAL path in the repo
      _selected_tests.txt        <- which test names in it this bucket owns
    fail2pass/
      path/to/test_file.py       <- the SAME file often appears in both buckets
      _selected_tests.txt
  source_row.json      raw dataset row, if generated by `evals scaffold` (audit trail)
```

- **pass2pass** — a regression tripwire. These already pass; a correct fix must not break them.
- **fail2pass** — the proof. These must fail on the baseline (showing the bug is real and
  the test detects it) and pass once it's fixed.
- **patch.diff** — must be a real unified diff. Generate it with `git diff`; hand-typing one
  is a reliable way to produce a patch that `git apply` rejects (a blank context line is a
  single space, not an empty line).
- **Why bucket directories mirror repo-relative paths, not just filenames:** at grade time
  the runner restores these files to their *original* locations in the repo, not an isolated
  staging directory — Go and Java test files must sit in their real package directory to
  even compile, and JS files using relative imports (`require('../src/x')`) stop resolving
  once moved. See [Supporting other languages](#supporting-other-languages).
- **Why each bucket also has `_selected_tests.txt`:** the same source file routinely holds
  tests from *both* buckets (SWE-bench selects individual test functions, not whole files),
  so once both buckets are restored to the same real path they can't be run separately. The
  harness instead runs `test_cmd` once and partitions the JUnit results by matching each
  test's name against the bucket that claims it. One name per line; `Class::test_name`-style
  entries are reduced to their trailing segment automatically.

---

## `task.json` reference

| Field | Required | Description |
|---|:--:|---|
| `task_id` | ✅ | Identity; forms the image tag `evals/<task_id>:<commit>` |
| `repo` | ✅ | Git URL to clone |
| `commit` | ✅ | Pinned SHA — the reproducibility anchor. `HEAD` in the image always equals this. |
| `deps_cmd` | ✅ | Install the project, e.g. `pip install -e .` |
| `test_cmd` | ✅ | Run tests. **Must contain `{path}` and/or `{dirs}`**; may contain `{report}`. |
| `hidden_paths` | — | Repo-relative files deleted from the image at `init` — the guardrail tests that already exist in the repo. |
| `protected_paths` | — | Test-infrastructure paths reset before grading (anti-tamper). Defaults to `*conftest.py`, `*pytest.ini`, `*tox.ini`, `*sitecustomize.py`. |
| `base_image` | — | Container image. Default `python:3.11-slim`. |
| `setup_cmd` | — | Root-level prep before cloning. **Must install `git`.** |

### `test_cmd` placeholders

| Placeholder | Substituted with | Used by |
|---|---|---|
| `{path}` | space-separated repo-relative test **file** paths | file-based runners: pytest, jest |
| `{dirs}` | space-separated `./`-prefixed parent **directories** of those files | package-based runners: `go test`, which can't compile a single test file in isolation |
| `{report}` | where the runner expects JUnit XML | any runner that needs an explicit output path |

**The one contract: `test_cmd` must produce JUnit XML.** The CLI never parses stdout. If
you omit `{report}`, `--junitxml=<path>` is appended for you, so plain pytest bundles need
nothing extra:

```jsonc
"test_cmd": "pytest {path} -q"                                        // pytest, XML auto-appended
"test_cmd": "JEST_JUNIT_OUTPUT_FILE={report} npx jest {path} --reporters=jest-junit"
"test_cmd": "go test -v {dirs} 2>&1 | go-junit-report -set-exit-code > {report}"  // {dirs}, not {path}
```

### `hidden_paths` — how tests stay hidden

The solver must never see pass2pass/fail2pass, but it **does** need to see every other test
in the repo — hide the whole test suite and you stop measuring "can it fix the bug" and
start measuring "can it work blind". So the bundle names the guardrail files explicitly and `evals init`
deletes exactly those from the image — the solver's container genuinely does not contain
them. At grade time (and at `validate`), the bundle's own copies are restored into a running
container, at their *original* repo-relative path, moments before `test_cmd` runs — they're
never baked into the image at all, and restoring to the real path (rather than an isolated
staging directory) is what makes this work for languages where a test file has to live in
its real package directory to compile or resolve relative imports.

fail2pass tests usually don't exist in the base repo (they're added by the fix), so they
generally need no `hidden_paths` entry.

---

## Command reference

### Core

```bash
evals init <bundle> [--force]
```
Builds `evals/<task_id>:<commit>`: clone at the pinned commit → strip `hidden_paths` →
run `deps_cmd` → freeze a `/baseline` snapshot → create a non-root user. Skipped if the
image already exists; `--force` rebuilds. Prints which paths were stripped.

```bash
evals validate <bundle> [--image TAG] [--keep-artifacts]
```
Runs both buckets on the untouched baseline and asserts the invariant: **pass2pass all
pass, fail2pass all fail.** Exits 1 if it doesn't hold. Run this before anything else.

```bash
evals run <bundle> --solver stub|oracle|llm [--provider P] [--model M]
                  [--temperature T] [--thinking minimal|low|medium|high]
                  [--append-prompt TEXT|@file] [--verbose]
                  [--image TAG] [--keep-artifacts] [--network NET]
```

| Flag | Purpose |
|---|---|
| `--verbose` / `-v` | Narrate each phase with elapsed times. Also available on `init` and `validate`. Worth using on Go/Java bundles: the test step compiles first and can look hung for minutes with no output. |
| `--temperature` | Default `0.0`, for reproducible runs. |
| `--thinking` | Reasoning effort, default `medium`. Higher is slower and eats the output budget — measured on one task, `high` used **8.8× the tokens for no accuracy gain**, so treat it as a hypothesis to test rather than a free upgrade. |
| `--append-prompt` | Extra guidance for the solver, inline or `@path/to/file`. **Additive only** — it is placed after the task description, repo files and output rules and can never replace them, so two runs of a task stay comparable. The text is recorded in the run's DB row, since a result means nothing without knowing what guidance produced it. |
Two containers, in sequence:
1. **Solve** — a fresh container with no hidden tests. The solver reads the repo and
   returns a diff. The diff is applied and the canonical change captured. Container destroyed.
2. **Grade** — a *new* fresh container. The captured diff is reapplied, protected paths are
   reset, both buckets' guardrail tests are restored to their real repo paths, `test_cmd`
   runs once, and the results are partitioned back into pass2pass/fail2pass by test name.

The diff is written to the database **between** these phases, so a crash never costs
another LLM call. Exits 0 only on `SOLVED`.

### Re-grading without re-solving

```bash
evals resume [run_id]                      # finish a run that died after solving
evals replay [run_id]                      # re-grade a finished run as a NEW linked run
evals grade  <bundle> --diff-file p.diff   # grade any externally produced patch
```

All three reuse the same grading path with **no solver and no LLM cost**. `replay` is how
you re-score stored diffs after fixing a harness bug; `grade --diff-file` mirrors
SWE-bench's predictions-file model, letting you evaluate a patch from another agent,
another harness, or a human.

### Everything else

```bash
evals scaffold --instance-id ID --out bundles/x   # fetch a dataset row live and generate a bundle
evals scaffold --row row.json --out bundles/x     # or from an already-saved row file
evals providers [--provider P] [--model M]        # check LLM credentials with one tiny call
evals history [--limit N]                         # recent runs
evals logs <run_id>                               # one run's log
evals clean [--all]                               # remove leftover containers (and images)
```

---

## Verdicts

After the solver runs, the *same* tests are judged against flipped expectations — both
buckets should now pass:

| pass2pass | fail2pass | Verdict | Meaning |
|---|---|---|---|
| any failed | anything | `REGRESSION` | Broke working code. Checked **first** — disqualifying however much of the bug was fixed. |
| all pass | all pass | `SOLVED` | Fixed, nothing broken. |
| all pass | some pass | `PARTIAL` | Incomplete fix. |
| all pass | none pass | `UNSOLVED` | Nothing broken, bug still present. |

A bucket also fails if an expected test is **missing** from the results — a deleted or
uncollectable test is a failure, not a silent pass.

`evals run`, `resume`, `replay`, and `grade` exit 0 only on `SOLVED`, so they drop into CI
unchanged.

---

## Configuring an LLM provider

Providers are pluggable. Selection order: **CLI flag → env var (`TASKCLI_PROVIDER`) →
default (`gemini`)**.

| Provider | Install | Credential | Default model |
|---|---|---|---|
| `gemini` | `pip install -e ".[gemini]"` | `GEMINI_API_KEY` ([free tier](https://aistudio.google.com/apikey)) | `gemini-3.1-flash-lite` |
| `anthropic` | `pip install -e ".[anthropic]"` | `ANTHROPIC_API_KEY` | `claude-opus-5` |
| `bedrock` | `pip install -e ".[bedrock]"` | standard AWS chain (`aws configure`) | `anthropic.claude-sonnet-5` |
| `openai` | `pip install -e ".[openai]"` | `OPENAI_API_KEY` | `gpt-4o` |

```bash
export GEMINI_API_KEY=...
evals providers --provider gemini        # verify before a real run
evals run bundles/toy-calc-001 --solver llm --provider gemini --keep-artifacts
```

**Or use a `.env` file** instead of exporting keys in your shell: copy `.env.example` to
`.env` in the repo root and fill in the key for whichever provider you're using. `.env` is
gitignored — never commit it. It's loaded automatically on every `evals` invocation; a real
exported environment variable always takes precedence over it.

`evals providers` makes one tiny call per provider and reports exactly what's blocking each
— missing package, missing key, no model access — so you fix configuration before spending
five minutes on a full run.

Adding a provider is a ~20-line class with one method (`complete(prompt) -> str`) plus one
registry entry in `solvers.py`. Nothing else changes.

**Where the API key lives:** on the host, never in a container. Containers run with
`--network none` and hold no credentials; the process holding the key runs no untrusted
code. This matches SWE-agent and OpenHands.

---

## Generating bundles from SWE-bench Pro

One command — no manual download step, no intermediate file:

```bash
# 1. Generate the bundle straight from the dataset (needs pip install -e ".[scaffold]")
evals scaffold --instance-id instance_future-architect__vuls-2c84be80b65d022c262956cd26fc79d8bb2f7010 \
    --out bundles/my-task

# 2. Run the ladder
evals init     bundles/my-task
evals validate bundles/my-task                   # must pass before going further
evals run      bundles/my-task --solver stub     # expect UNSOLVED
evals run      bundles/my-task --solver oracle   # expect SOLVED
```

`--instance-id` fetches the row directly from HuggingFace — via the official `datasets`
library in streaming mode, so it never downloads the full dataset, just reads forward
through the split until the id matches. (`--row row.json` still works too, if you already
have a row saved as JSON — same as before.)

`scaffold` then clones the repo at the pinned commit, applies the dataset's `test_patch` to
materialize the guardrail tests (fail2pass tests usually don't exist in the base repo),
splits `FAIL_TO_PASS`/`PASS_TO_PASS` into buckets (mirroring each test file's real repo
path — see [The bundle format](#the-bundle-format)), derives `hidden_paths`, and writes
`task.json`, `description.md`, `patch.diff`, and `source_row.json`.

It uses two more dataset columns that remove most of the remaining guesswork:

- **`dockerhub_tag`** → `base_image: jefzda/sweap-images:<tag>`, the dataset's prebuilt
  per-instance image with the toolchain and dependencies **already installed**. This is the
  default; `--build-image` instead builds from a plain language base image (`python:3.11-slim`,
  `node:20`, `golang:1.22`).
- **`before_repo_set_cmd`** → the guardrail test file paths. Its `git checkout <sha> -- <paths>`
  line names them explicitly, which matters for Go and JS rows whose test IDs contain no
  file path at all.

Overrides: `--deps-cmd`, `--test-cmd`, `--build-image`, `--dataset`, `--split`.

---

## Querying the run database

Every command writes a row to `runs.db` (SQLite, stdlib only). It's an execution ledger,
not just a log — the solver's diff is a first-class column, which is what makes
`resume`/`replay` possible.

```bash
evals history --limit 20
evals logs 42
```

```sql
-- runs table
run_id, created_at, updated_at, command, task_id,
status,          -- RUNNING | PATCH_CAPTURED | SUCCESS | ERROR   (lifecycle)
verdict,         -- SOLVED | PARTIAL | UNSOLVED | REGRESSION     (outcome)
args_json, solver_name, provider, model_id,
captured_diff, patch_applied, patch_error,
results_json, artifacts_dir, source_run_id, log
```

Raw SQL, no extra tooling needed:

```bash
python -c "import sqlite3,json; r=sqlite3.connect('runs.db').execute(
  'SELECT results_json FROM runs WHERE run_id=?',(42,)).fetchone();
  print(json.dumps(json.loads(r[0]),indent=2))"
```

Older databases are migrated in place on open (`ALTER TABLE`), so upgrading never requires
deleting `runs.db`.

---

## Artifacts

Two different things, for two different questions:

| | `reports/` — *what happened* | `artifacts/run-<id>/` — *why it happened* |
|---|---|---|
| Contents | verdict, per-test outcomes, captured diff, solver metadata | LLM prompt, raw response, raw + captured diffs, per-bucket JUnit XML, test logs with the exact command, the generated Dockerfile |
| Written | always | only with `--keep-artifacts` |
| Lifecycle | overwritten per (task, solver) | new directory per run id |

When an LLM diff fails to apply, `artifacts/run-<id>/solver_raw_response.txt` usually shows
why immediately — prose before the diff, wrong path prefixes, a hallucinated file.

---

## Supporting other languages

Nothing in the harness is Python-specific. Point the four knobs at another ecosystem:

```jsonc
// Node
{ "base_image": "node:20",
  "setup_cmd": "apt-get update && apt-get install -y --no-install-recommends git && npm i -g jest-junit",
  "deps_cmd":  "npm ci",
  "test_cmd":  "JEST_JUNIT_OUTPUT_FILE={report} npx jest {path} --reporters=jest-junit" }

// Go - note {dirs}, not {path}: `go test` takes package directories, not files
{ "base_image": "golang:1.22",
  "setup_cmd": "apt-get update && apt-get install -y --no-install-recommends git && go install github.com/jstemmer/go-junit-report/v2@latest",
  "deps_cmd":  "go mod download",
  "test_cmd":  "go test -v {dirs} 2>&1 | go-junit-report -set-exit-code > {report}" }
```

`evals scaffold` picks these defaults automatically from the dataset's `repo_language`.

**Current status, stated plainly:** both bundle *generation* and *execution* are proven
end-to-end for Python and **Go** — `bundles/vuls-redhat-001` is a real SWE-bench Pro Go
instance that validates, oracle-solves to `SOLVED`, and stub-solves to `UNSOLVED`, same as
the Python bundles. JS/TypeScript has a complete profile and is unit-tested, but has never
been run against a real container. Java has no profile yet at all. Adding a language means
supplying three things: a base image + JUnit-reporter setup command, the test invocation
(`{path}`/`{dirs}`), and — if the language's test-id format isn't one of the three already
handled (pytest, `go test`, Jest) — a small function mapping that format's ids to what the
JUnit report will actually contain. See `DESIGN_NOTES.md` §4 and §8 for the full story,
including a real bug this surfaced that had nothing to do with language support at all.

---

## Design tradeoffs

Every choice below had a viable alternative. This section is what was chosen, what was
rejected, and why. Longer versions with the failures that produced them are in
[`DESIGN_NOTES.md`](DESIGN_NOTES.md).

### Why containers at all?

The naive workflow — a git branch with the tests removed, run the LLM, add the tests back —
fails for two reasons. It's **slow** (every task re-resolves dependencies) and it's
**brittle** (repo A needs Python 3.8 + pytest 6, repo B needs Go 1.22, repo C needs Node 20;
they collide on one machine).

A container pins the OS, the toolchain, the dependencies, and the repo state in one
artifact addressed by `evals/<task_id>:<commit>`. Build once, run in ~1 second thereafter.
It also gives isolation for free — which matters, because we are about to execute code an
LLM wrote.

**Rejected: virtualenvs / language sandboxes.** They isolate *packages*, not system
libraries, compilers, or the filesystem. `apt-get` dependencies and Go toolchains don't fit,
and a solver could still write to your home directory.

### Why two containers instead of one?

**The governing constraint: the environment that produces a patch must never be the
environment that grades it.**

Container A (solve) has the graded tests deleted, no network, no API key. Container B
(grade) is **fresh**, gets the diff re-applied, and only then sees the tests. Only a unified
diff crosses between them.

**Rejected: one container, hide the tests then reveal them.** Hiding is necessary but not
sufficient. A solver could leave a mutated `conftest.py`, a `sitecustomize.py`, or a stray
`.pyc` behind; state accumulated during solving would still be live during grading. A fresh
container makes that class of problem structurally impossible rather than something you
have to enumerate and defend against. (We still reset `protected_paths` in B — belt *and*
braces, and it's the anti-tamper measure SWE-bench's own harness uses.)

**Cost:** one extra container start per run, a few seconds. Cheap.

### Why does the LLM call happen on the host, not in the container?

The API key never enters a container. Container A runs with `--network none` and holds no
credentials; the host process that holds the key executes no untrusted code. Neither side
has both the secret and the ability to misuse it.

There are three ways to arrange this, and it's worth being explicit about all of them because
the choice looks arbitrary until you see what each costs.

**A — Host holds the key, container only executes (what this harness does).**
The host reads files out of the container over `docker exec`, builds the prompt, calls the
provider, and pipes the resulting diff back in over stdin. The container stays on
`--network none` for its entire life.

**B — Agent runs inside the container, key injected with `-e`.**
Rejected. `-e` is the correct *mechanism* for giving a container a secret; the problem is
*which* container. This is the designated place where untrusted, model-authored code runs.
It also needs network access, removing the strongest isolation guarantee we have — and
prompt injection from a hostile repo to key exfiltration becomes a single step. Secondarily
it breaks language-agnosticism: every task image, in every language, would need Python and a
provider SDK just to make one HTTPS call.

**B′ — Agent inside the container, key held by a proxy sidecar.**
The honest version of B. The container routes requests through a sidecar that injects the
credential and forwards only to allowed provider endpoints, so it never holds the secret and
never reaches the open internet. This is a real, correct design — it's just meaningful
infrastructure for no benefit while the solver is single-shot and executes no repo code.

**C — Don't hand-roll the execution layer at all.**
This is a solved problem, and worth naming rather than reinventing:

| Tool | What it gives you |
|---|---|
| [**SWE-ReX**](https://github.com/SWE-agent/SWE-ReX) | The runtime layer under SWE-agent. One interface for running commands in a sandbox — local, Docker, AWS Fargate, or Modal — with the agent code unchanged across all of them. Runs a FastAPI server inside the sandbox, handles interactive shells (ipython, gdb), parallel sessions, and detects command completion by appending a sentinel to each command and watching stdout for it. |
| [**OpenHands runtime**](https://docs.openhands.dev/openhands/usage/architecture/runtime) | The same shape, with an event-stream architecture and its own container lifecycle management. |
| **Modal / E2B / Daytona** | Hosted sandboxes — the same abstraction as a managed service, with per-instance isolation and horizontal scale. |

Our `docker_mgr.py` (191 lines) plus `ContainerFiles` is, honestly, a minimal
reimplementation of a slice of what SWE-ReX does properly. That is a deliberate trade for a
single-shot harness: we need exactly three operations — read files, apply a diff, run a test
command — and a subprocess wrapper over the docker CLI is auditable in one sitting, adds no
dependency, and needs no server process inside the image.

**But it does not scale to the agentic case, and that's the honest limit.** The moment the
solver needs an interactive shell, multiple parallel sessions, reliable "has this command
finished?" detection, or a cloud backend, the correct move is to adopt SWE-ReX rather than
grow this file. All three of A, B′ and C keep the same invariant — *the credential and the
untrusted code never sit in the same place* — which is the property that actually matters.

Design A is also what SWE-agent and OpenHands do at the top level: the agent loop and the
key live outside the sandbox, and the sandbox is driven over an exec interface.

### Why a single-shot diff instead of an agentic loop (SWE-agent / SWE-ReX)?

**What we built:** one prompt in, one unified diff out, graded. No tools, no retries, no
turns.

**What SWE-agent/SWE-ReX and OpenHands do:** give the model a shell inside the container and
let it explore, edit, run tests, read the failure, and revise — typically up to ~50 turns.
That is measurably stronger. Scale evaluates SWE-bench Pro using the SWE-agent scaffold for
exactly this reason.

**Why single-shot here:**

1. **The unit under measurement is a patch, not a conversation.** A solver is anything that
   turns a problem statement into a diff — a stub, the golden patch, or an LLM. Keeping that
   contract narrow is what lets all three be graded by identical machinery.
2. **Reproducibility.** A loop makes runs *less* reproducible, not more — every turn adds a
   branch point.
3. **Cost and comparability.** One inference call per run makes two runs of the same task
   comparable and cheap. A 50-turn agent costs 50× and its trajectory is unrepeatable.
4. **It isolates the variable being measured.** The harness is the deliverable; a loop would
   make it hard to tell a harness bug from an agent-strategy bug.

**The honest cost:** every LLM failure observed on the hard Go bundle — a double-applied
epoch, a nil-receiver panic, two compile errors — would be caught by **one** compiler or
test run. A bounded revision loop (apply → build → feed the error back → one revision) is
the single highest-value extension, and the plumbing already exists: `ContainerFiles` reads
via `docker exec`, and the baseline-snapshot diff capture already works. It is deliberately
scoped out, not overlooked. See [`DESIGN_NOTES.md`](DESIGN_NOTES.md) §10.

### Why a unified diff rather than SEARCH/REPLACE blocks?

This one was built the other way first, and reverted.

Asking a model for a diff means asking it to reproduce byte-exact context, count hunk lines,
and emit correct headers — and real runs failed on all three. So the harness switched to
SEARCH/REPLACE blocks and computed the diff itself with `difflib`.

That made the **harness** the thing that was almost-right: five self-inflicted parser bugs in
quick succession, each rejecting model output that was, on inspection, correct. The failure
had moved to the worse side — a rejection now meant *our* parser disagreed, with no external
authority to appeal to.

> **The principle that came out of it: prefer the contract where a failure is unambiguously
> attributable to the subject under test, not to the harness.**

`git apply` is a neutral adjudicator nobody has to trust us about, unified diff is the most
heavily represented edit format in any model's training data, and the stored artifact is a
normal patch a human can apply by hand. Aider reached the same conclusion by measurement
(GPT-4 Turbo: 20% with SEARCH/REPLACE, 61% with unified diffs).

The original three failure modes are handled by **semantics-preserving repair** —
restoring a missing leading space, recomputing `@@` counts from the hunk body, stripping
wrapper tags — plus a bounded apply ladder ending in `patch --fuzz=5`, which is what
SWE-bench's own harness falls back to. None of those can change what a patch *does*.

### Why JUnit XML instead of parsing test output?

One contract, and it's the thing that makes this not-Python-only. SWE-bench writes per-repo
log parsers; we never touch stdout. Adding a language becomes a `task.json` change
(`base_image`, `setup_cmd`, `deps_cmd`, `test_cmd`) rather than a code change.

**Rejected: per-runner stdout parsers.** N parsers to write, each breaking on a runner
version bump. **Rejected: exit codes only.** Tells you *something* failed, not *which* test —
and the whole point is per-test attribution.

**The cost:** a runner with no JUnit reporter needs one added in `setup_cmd` (the Go bundle
installs `go-junit-report`; a JS bundle would install `jest-junit`).

### Why SQLite, and why a ledger rather than a log?

Zero-setup, single file, stdlib-only, and every collaborator can query it without
infrastructure. WAL mode lets `evals history` read while a run writes.

It's a **ledger**, not a passive log: the solver's captured diff is a first-class column
written *at the moment it exists*, before grading. That single checkpoint is what makes
`evals resume` and `evals replay` possible without paying for inference twice — which is not
just a cost saving. Since LLM output is not reproducible (see below), re-running does not
reproduce the thing you're trying to re-examine; only the stored artifact does.

`status` and `verdict` are deliberately separate columns. Conflating them makes "did this
crash?" unanswerable for a run that produced a verdict, and leaves `init`/`validate` — which
have a lifecycle but no solver verdict — with nowhere to sit.

**Rejected: Postgres** (infrastructure for a single-user CLI). **Rejected: JSON-lines**
(no indexed query by run id, which `logs`/`replay`/`resume` all depend on). **Rejected: full event
sourcing** — only one checkpoint is load-bearing; a six-state machine would be ceremony
around a single durable write.

### Why capture the diff by snapshot rather than `git diff HEAD`?

The hidden tests are deleted from the working tree but still present in `HEAD`. Any
git-history-based diff therefore reports them as **deleted**, that deletion gets captured
into the patch, and re-applying it in the grading container would delete the very tests
about to grade it.

Diffing a frozen `/baseline` against a fresh `/current` copy avoids this entirely — both
sides agree the file doesn't exist. It also means `HEAD` never moves, so the pinned commit
stays the reproducibility anchor and no synthetic commits are created.

### What is *not* reproducible, stated plainly

Pinned and deterministic: the commit, the image tag, the baseline snapshot, bucket
attribution, and the verdict function. **Re-grading the same diff always yields the same
verdict** — that's what `evals replay` exercises.

**`temperature=0` does not make an LLM reproducible.** Measured here: two runs sent
byte-identical 346,614-byte prompts and returned materially different patches with different
verdicts. Any conclusion drawn from a single sample per prompt variant is measuring noise.
This is why the ledger stores diffs, and why prompt changes in this repo are defended by
mechanism rather than by a verdict delta.

### Known limitations

- **The image tag has no harness fingerprint.** `evals/<task_id>:<commit>` doesn't change
  when the Dockerfile template does, so `init` skips a rebuild and a stale layout can survive
  a harness fix. Use `--force`. Hashing the rendered Dockerfile into the tag is the fix.
- **Symbol ranking over-matches on generic identifiers.** A description naming `TypeError`
  or `MutableMapping` grep-matches ~100 files, filling the prompt budget. `ansible-vars-001`
  solves reliably regardless, but its prompt is larger than it needs to be.
- **`validate` doesn't run the golden patch.** A `--with-oracle` flag asserting fail2pass
  actually flips would be a language-agnostic canary.
- **No automated tests for `resume`, `scaffold`, `providers`, `logs`, `history`** — they're
  exercised manually. The other six commands are covered by `tests/linux/run_linux_suite.sh`.
- **Container escape is out of scope.** The threat model is an *optimising* solver, not a
  hostile one.

---

## Testing

```bash
# Fast, dependency-free unit checks (ledger migration, diff normalization, id-attribution
# logic for all three supported test-id formats, language-profile contracts)
python tests/unit_checks.py

# Integration on your machine
evals validate bundles/toy-calc-001
evals run bundles/toy-calc-001 --solver oracle    # must be SOLVED
evals run bundles/vuls-redhat-001 --solver oracle # Go bundle - must also be SOLVED

# Full suite on Linux, driving your Docker daemon from inside a container
docker build -f tests/linux/Dockerfile -t evals-linux-test .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    evals-linux-test sh tests/linux/run_linux_suite.sh
```

The Linux suite covers 16 checks: bundle parsing, ledger migration from an older schema,
diff normalization, the full init/validate/run flow, replay and external-patch grading,
the anti-tamper guard, error handling, and container cleanup. It's how cross-platform
behavior is verified rather than assumed.

---

## Troubleshooting

**`'docker' was not found on PATH`** — install Docker and confirm `docker info` works.

**`task.json field 'test_cmd' contains a Windows-style path`** — Git Bash rewrote a
container path (`/workspace/...` → `C:/Program Files/Git/workspace/...`). Prefix the
command with `MSYS_NO_PATHCONV=1`, use PowerShell, or edit `task.json` directly.

**`no JUnit XML produced for the <bucket> bucket`** — `test_cmd` didn't write the report.
Either use a pytest command (XML is appended automatically) or place `{report}` yourself.

**`validate` fails on a new bundle** — usually `deps_cmd` or `test_cmd`. Run
`evals validate --keep-artifacts` and read `artifacts/run-<id>/<bucket>.log`, which contains
the exact command and its full output.

**`diff did not apply cleanly`** — for `--solver llm`, inspect
`artifacts/run-<id>/solver_raw_response.txt`. For a hand-written `patch.diff`, regenerate
it with `git diff` rather than editing by hand.

**`GEMINI_API_KEY is not set`** — get a free key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) and `export GEMINI_API_KEY=...`.

**Credentials rejected even though `.env` looks right** — an exported shell variable takes
precedence over `.env`. The CLI prints a `note:` when the two disagree; clear the stale one
with `Remove-Item Env:\GEMINI_API_KEY` (PowerShell) or `unset GEMINI_API_KEY` (bash).

**A run seems to hang with no output** — likely `go test` compiling, which is silent and can
take minutes. Re-run with `--verbose` to see each phase, or check `docker stats` in another
terminal: high CPU means it's compiling, not stuck.

**Rate limited (`429`)** — free-tier quotas are **per model per day**, so switching is usually
faster than waiting: `--model gemini-3.1-flash-lite` has a far higher allowance than
`gemini-2.5-flash`. A completed run's diff is already stored, so `evals replay <run_id>`
re-grades it without spending another request.

**Bedrock `AccessDeniedException`** — the IAM user needs `bedrock:InvokeModel` and the
model must be enabled in the Bedrock console under *Model access* for your region. List
usable IDs with `aws bedrock list-foundation-models`. If your account can't grant this,
`--provider gemini` (the default) or `--provider anthropic` avoid IAM entirely.

**Leftover containers** — `evals clean` (or `evals clean --all` to drop images too). Normal
runs clean up after themselves, including on failure.

---

## License

The harness — everything under `agent_evals/` and `tests/` — is MIT licensed; see
[`LICENSE`](LICENSE).

The task bundles are not mine and are not covered by that license. `bundles/toy-calc-001` is
a hand-written toy. `bundles/ansible-vars-001` and `bundles/vuls-redhat-001` are derived from
rows in the [SWE-bench Pro dataset](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)
(CC-BY-4.0) and vendor test files from the upstream projects they point at — Ansible
(GPL-3.0) and Vuls (GPL-3.0) — which remain under their original licenses, held here only so
a bundle is self-contained and reproducible.
