# Design Notes

Key tradeoffs behind the Task Bundle CLI. Most of these were forced by a bug or a failed
run rather than chosen up front, so each one records what went wrong and what changed.

---

## 1. Two containers, because the solve environment can't be trusted to grade itself

`evals run` uses two containers strictly in sequence. Container A is where the solver acts:
it has the repo but no guardrail tests, no network, and no API key. It produces a patch,
and is then **destroyed**. Container B starts fresh from the same image, applies only that
patch, and grades.

The obvious cheaper design is one container — solve and grade in place. It's wrong, and the
reason generalizes: container A is contaminated by definition, because untrusted code ran
there. A solver could leave a modified `conftest.py`, a `sitecustomize.py` that patches
assertions, stale bytecode, or a monkeypatched runner. Grading there means grading inside
an environment the graded thing controlled. The two-container split makes the boundary
explicit: **the only thing crossing from A to B is text.** Today's solvers just return a
diff, so the split is arguably more than strictly needed — it earns its keep the moment a
solver can execute commands, which is the direction agentic solvers are going, and it costs
about a second.

This mirrors how the reference systems split responsibilities. The official SWE-bench
harness separates inference from evaluation entirely (models write a predictions file; the
harness only grades). SWE-agent and OpenHands both keep the agent loop and the API key
*outside* the sandbox and drive the container over exec. We converged on the same shape and
then corrected the two places we'd diverged from it.

---

## 2. Hiding tests is necessary but not sufficient — the anti-tamper reset

The solver must never see the pass2pass/fail2pass tests, but it **does** need to see every
other test. Inference can't tell which repo files are the guardrails, so `task.json`
names them (`hidden_paths`) and `evals init` deletes exactly those from the image. An earlier
version deleted the whole test directory — simpler, but it over-hid and violated the second
half of the requirement.

Then a harder problem surfaced. The solver's diff is applied *wholesale* before grading, so
a solver can influence its own grade through files that aren't tests at all. This patch
scores `SOLVED` on a completely unfixed repo:

```python
# conftest.py — pytest auto-loads this, no import needed
def pytest_collection_modifyitems(config, items):
    for item in items:
        item.obj = lambda *a, **k: None      # every test body becomes a no-op
```

A test that does nothing doesn't raise; a test that doesn't raise passes. `conftest.py`
isn't in `hidden_paths` — it isn't a test file — so hiding never touched it. We measured it:
identical patch, guard off → **SOLVED**; guard on → **UNSOLVED**. Before this change the
harness would have scored that as a success.

The fix is borrowed from SWE-bench's own eval script, which resets test files from git
before running. After applying the patch, we `git checkout HEAD -- <protected_paths>` and
`git clean` anything added. The bug fix survives; the tampering is erased. We *also* record
that the patch touched those paths, because an attempted tamper is a signal worth surfacing
rather than silently undoing. The defaults are deliberately narrow — `conftest.py`,
`pytest.ini`, `tox.ini`, `sitecustomize.py` — and exclude `setup.cfg`/`pyproject.toml`,
which can carry pytest config but which legitimate fixes also touch; resetting those by
default would break real solutions.

---

## 3. Capturing the diff without moving `HEAD`

Capturing what the solver changed looks like a job for `git diff HEAD`. It isn't. `init`
already deleted `hidden_paths`, so the working tree is dirty relative to `HEAD`, and
`git diff` would fold our own housekeeping deletion into the solver's patch. Committing the
stripped state fixes that but moves `HEAD` off the commit declared in `task.json` — which
breaks the reproducibility contract, since anyone inspecting the container would find a SHA
that doesn't match the bundle.

So at build time we freeze a `.git`-free copy of the tree into `/baseline`, *after*
`deps_cmd`. Capturing is then a pure filesystem comparison: copy the workspace to
`/current`, strip `.git`, and `git diff --no-index /baseline /current`. `HEAD` never moves;
no synthetic commits exist.

Three bugs made this subtle. Symlinking the two directories to `a`/`b` made git diff the
*links* instead of their contents. Diffing the live workspace swept `.git` internals in as
new files. And freezing `/baseline` *before* `deps_cmd` meant `pip install -e .` artifacts
showed up as phantom additions that then failed to reapply in container B, which already had
its own copy. The resulting headers carry an extra path component, so we normalize them back
to ordinary `a/`/`b/` form — which also means every diff applies with plain `-p1` and a
stored patch is one a human can `git apply` by hand.

---

## 4. JUnit XML as the language contract — a deliberate divergence

The harness never parses stdout. `test_cmd` must produce JUnit XML, and that is the *only*
thing the runner knows about testing. Combined with `base_image`, `setup_cmd`, and
`deps_cmd`, supporting a new language is configuration rather than code.

This is where we knowingly differ from SWE-bench, which parses stdout with per-repo,
per-framework log parsers. The research justifies the divergence: Multi-SWE-bench (8
languages) needs "adaptive log parsers — including LLM-generated scripts" that "fall back to
LLM code generation if regex parsing fails," and reports that in some Java projects "test
outputs from concurrent threads are interleaved without delimiters, making rule-based log
parsing infeasible." They need a neural network to write parsers, and it still breaks on
Java. We can afford XML because we control the image through `setup_cmd` — installing a
reporter is one line — whereas SWE-bench ingests thousands of repos and can assume nothing.
The Java case that defeats their parser is exactly where Surefire's native XML is easiest.
The cost is a dependency in the image; the escape hatch is a bundle-supplied wrapper script.

**Current state:** bundle generation and execution are both proven for Python and Go; Node
and Java remain configuration-only (implemented, unit-tested, never run against a real
container). The runner restores guardrail tests to their *original* repo-relative paths
before running `test_cmd` once — not the isolated `/workspace/_hidden/<bucket>/` staging an
earlier version used, which broke Go and Java (must compile in their package directory) and
JS (relative `require('../src/x')` stops resolving once a file moves). Restoring in place is
what SWE-bench does, and what the dataset's own `before_repo_set_cmd` spells out
(`git checkout <sha> -- <test files>`). Because pass2pass and fail2pass routinely live in
the *same* file, the two buckets can no longer be executed separately once restored — so
bucket separation moved from execution to attribution: one test run, then partition the
JUnit results against each bucket's `_selected_tests.txt`. Verified on a Go instance
(`future-architect/vuls`) scaffolded straight from SWE-bench Pro: `go test` output through
`go-junit-report`, Go subtest ids (`Test_x/case_name` — the `/` is a subtest separator, not
a path) correctly attributed, `validate`/`oracle`/`stub` all behaving exactly as they do for
the Python bundles.

---

## 5. The database as an execution ledger, not a log

The solver's diff is the expensive, non-reproducible artifact of a run: it costs money and
won't come back identical. So it is written to SQLite as a first-class column **the moment
it exists**, before grading starts, rather than bundled into a results blob at the end.

That single change buys three commands that all share one grading path: `evals resume`
finishes a run that died after solving; `evals replay` re-grades a stored diff as a new
linked run; `evals grade --diff-file` scores a patch from anywhere at all. Fix a bug in the
grading logic and you can re-score fifty stored diffs deterministically for free instead of
re-running inference. This is the same principle as SWE-bench's predictions file, which is
what makes their leaderboard independently verifiable.

Two smaller choices. `status` (lifecycle: `RUNNING`/`PATCH_CAPTURED`/`SUCCESS`/`ERROR`) is a
separate column from `verdict` (`SOLVED`/`PARTIAL`/`UNSOLVED`/`REGRESSION`) — conflating
them makes "did this crash?" unanswerable for a run that produced a verdict, and leaves
`init`/`validate` with nowhere to sit. And `update_run` writes through a fixed column
allowlist rather than interpolating caller keys into SQL; callers are internal today, but
that's the pattern that becomes an injection bug the first time a key comes from elsewhere.
Older databases migrate in place via `ALTER TABLE`, so upgrading never means deleting
`runs.db` — a constraint that only surfaced when the real database failed with
`NOT NULL constraint failed: runs.ts` while the synthetic migration test passed, because the
test had recreated a schema that never existed in the wild.

---

## 6. Isolation: each side gets exactly one dangerous capability

Untrusted code executes only inside containers, which run `--network none`, capped at 2GB,
as a non-root user. The LLM call happens on the host, where the API key lives. So the
process holding credentials runs no untrusted code, and the container running untrusted code
has neither credentials nor network. Putting the solver inside the container — the intuitive
"nothing on the host" position — would collapse both capabilities into one context, and the
moment solving becomes agentic, prompt injection from a malicious repo turns into
exfiltration in one step. No major system does this: SWE-agent reads `OPENAI_API_KEY` from
the host environment, and OpenHands keeps keys in a controller outside the sandbox. The
documented path for agentic solving is a reverse-proxy sidecar that injects the key at
egress, so the container still never holds it.

Being precise about what this does *not* guarantee: the container is isolated as an
*execution environment*, not as an *information source*. Container-produced data — the
captured diff, JUnit XML, test output — crosses to the host by design, because that's the
product. What's guaranteed is that no untrusted code runs on the host and that the container
cannot choose where host bytes land. The residual risks are named rather than papered over:
`xml.etree` is vulnerable to entity-expansion attacks (mitigated by rejecting any JUnit XML
containing `<!ENTITY`, since legitimate output never declares entities), and stdout is read
into memory before we can truncate it. An earlier design extracted the entire repo to a host
temp directory; that's now gone in favor of reading only the files the prompt inlines over
`docker exec`, which removed the host-disk landing entirely along with the tar/symlink
machinery it required.

---

## 7. Reproducibility, and what's deliberately out of scope

The commit is pinned, the image tag is `evals/<task_id>:<commit>`, and the environment is
baked at build time — same bundle, same tag, same environment on any machine, with
`validate`/`run` starting in about a second. `HEAD` inside the image always equals the
declared SHA. What is *not* deterministic: `deps_cmd` resolving floating dependency versions
over time, and LLM output itself. The first is the bundle author's choice (pin your
dependencies); the second is what `replay` exists to work around.

That second point turned out to be stronger than assumed, and it is worth stating precisely
because it was assumed wrongly here for a while. **`temperature=0` does not make an LLM
solver reproducible.** Two `run` invocations of the same bundle sent byte-identical prompts —
346,614 bytes each, verified by comparing the stored `solver_prompt.txt` files — and returned
materially different patches: one graded `PARTIAL`, the other `REGRESSION`.

Earlier in the same work, two other runs *had* come back byte-identical, and that was taken as
evidence of determinism. It was luck, not a property. The cost of believing it was real: a
sequence of prompt changes were each credited or blamed for a verdict change measured from a
single sample per variant, when the run-to-run variance on one fixed prompt is already as
large as anything observed between prompts. Several intermediate conclusions in this project
were therefore noise, including a confident claim that a particular checklist rule had "fixed"
a specific failure.

Two things follow. First, honest prompt evaluation needs N runs per variant and a
distribution, not a verdict — which the ledger already has the data for and only lacks a
summary command over. Second, and more structurally: **the only reproducible thing about an
LLM run is the artifact it already produced.** That is the actual justification for
`captured_diff` being a first-class ledger column written at the moment the diff exists, and
for `replay` re-grading a stored patch rather than re-running inference. Re-running is not
merely expensive, it does not reproduce the thing you are trying to re-examine.

Everything downstream of the model *is* deterministic, and that is where the harness's
reproducibility claim actually lives: the same diff, re-graded, always yields the same
verdict. `evals replay` exercises exactly that property, and it is how the harness was
re-validated after each bug fix without paying for inference again.

Deliberately not built, and documented instead: agentic solving with a proxy sidecar; batch
parallelism (the design supports it — UUID container names, per-task image tags, no shared
state, WAL-mode SQLite — the real constraints are memory at 2GB × N and per-run report
paths); and full event-sourcing, where only one checkpoint (`PATCH_CAPTURED`) is actually
load-bearing and a six-state machine would be ceremony around a single durable write.

The `oracle` solver deserves a closing note, because it's the reason most of the above got
found. It pushes a known-correct patch through the entire untrusted pipeline. If oracle
isn't `SOLVED`, the harness is broken rather than the model — there's no AI judgment call to
second-guess. Nearly every bug in these notes was caught that way: the CRLF corruption, the
`docker cp` symlink failure on Windows, all three snapshot-timing bugs, and the malformed
hand-written patch. It costs nothing to run and it is the single most useful debugging tool
in the project.

---

## 8. A wrong diagnosis, and the discipline that caught it

Proving the Go path required a third-party image (`jefzda/sweap-images`, SWE-bench Pro's
community-run mirror of per-instance prebuilt images). Starting a container from it failed:
`docker exec` against it returned "container is not running." The first diagnosis was a
corrupted pull — the image was multi-GB, an earlier pull attempt this session had been
interrupted, and "re-download the layers" is a plausible story for exactly this symptom. It
was wrong, and it cost real time before that became clear.

What actually caught it was refusing to trust a single-session diagnosis: pulling the same
tag independently, on a different machine, and getting an *identical* failure on an
*identical* digest. Content-addressed storage makes that decisive — matching digests means
matching bytes, so "bad pull" and "something specific to this session's Docker state" were
both eliminated in one step. That reframed the question from "why is this download broken"
to "why does this specific, correctly-downloaded image fail to start," which is a much
smaller and more tractable question.

The actual mechanism, once looked for directly: `docker run <image> sleep infinity` — how
every container in this harness starts — and the image declares `ENTRYPOINT ["/bin/bash"]`.
Docker's rule is that a command given on `docker run` is appended as *arguments* to an
existing `ENTRYPOINT`, not executed in its place, unless `--entrypoint` overrides it. So the
container's actual startup command was `/bin/bash sleep infinity` — bash treats its first
positional argument as a script filename when it isn't `-c`, finds the ELF binary `sleep` on
`PATH`, and fails trying to read it as text. `python:3.11-slim`, the base image behind every
previously-working bundle, sets no `ENTRYPOINT`, so `docker run` executes the given command
directly and the bug had never had an image to trigger it. Confirmed by directly comparing
`docker run --rm <image> sh -c "echo hi"` (fails) against `docker run --rm <image> -c "echo
hi"` (succeeds, because the baked-in `/bin/bash` entrypoint consumes `-c` correctly without
the redundant leading `sh`) — which pinpoints the entrypoint as the mechanism, independent
of any theory about the image's content.

The fix is in `DockerManager.run_detached()`: always pass `--entrypoint <command[0]>` when a
command is given, with the remainder as its arguments. Container startup is then
deterministic no matter what a base image's `Dockerfile` declares, and doesn't even depend
on a shell existing at startup — the harness's real commands run later via `docker exec`,
which bypasses `ENTRYPOINT` entirely and only cares about the container already being alive.

The general lesson: a harness built to run *arbitrary* third-party images can't assume a
base image is entrypoint-neutral, the way `python:3.11-slim` happens to be. It has to own
container startup unconditionally, or inherit whatever assumptions the image's author made
about how it would be invoked.

---

## 9. Five real LLM failures, one root cause, and a working end-to-end run

Every live call below is a real `gemini-2.5-flash` run against a real SWE-bench Pro instance.
They are recorded in the order they were found because the order is the lesson: four of them
looked like independent bugs in different parts of the pipeline, and all four turned out to be
downstream of one thing — the model was never shown the code it was asked to change.

The end state is `SOLVED` on `ansible-vars-001` (15 pass2pass + 1 fail2pass, `ansible/ansible`).

**Failure 1: a missing character, not a missing capability.** The model's diff had one
unchanged context line with no leading space — unified diff format requires every hunk line
to start with space/`+`/`-`/`\`, and this one had none, so `git apply` rejected the whole
patch as corrupt. This is a well-documented cross-provider failure mode: a language model
doesn't intuitively feel that "here is the code, unchanged" needs a prefix character the way
an addition or deletion obviously does. The fix is narrow and mechanical — `_extract_diff()`
now walks each hunk and restores the missing space on any line that isn't already correctly
prefixed — and it does *not* try to repair anything beyond exact formatting: a hunk whose
declared line counts don't match its own body, or a hunk that doesn't match the real file's
content, still correctly fails to apply. Repairing format is not the same as accepting
incorrect diffs, and the two must not be conflated.

**Failure 2: the right file lost a budget race, despite being correctly ranked.** The bundle's
description named the bug entirely by symbol (`combine_vars`, `VarsWithSources`) and — in the
dataset's own `interface` field — an explicit `Location: `lib/ansible/vars/manager.py`` `
pointer, three times over. Tracing the actual prompt: the relevance heuristic (does a
candidate file's own name appear in the description text) correctly ranked that file **#37 of
1001**, comfortably inside the top-300 candidates considered. It still never made it into the
prompt — only 21 files fit before the 120KB content budget ran out, because several files
ranked *ahead* of it were themselves large and only coincidentally matched the same weak
signal (e.g. `lib/ansible/config/manager.py` — an unrelated, sprawling file whose own name
also happens to contain "manager.py"). The model then wrote a diff against the real file from
its own training-data memory of a popular open-source project — syntactically fine, factually
wrong.

This is not a context-window problem — every provider here has far more room than 120KB —
it's budget-starvation among *correctly-ranked* candidates in a large repo, where a handful of
oversized, weakly-matching files can crowd out the one file that actually mattered. Raised the
budget from 120,000 to 400,000 bytes: roughly 3x the headroom, still nowhere near any
provider's real ceiling. This reduces the odds of this specific starvation pattern
substantially; it is not a proof that it can't recur on an even larger repo with even more
coincidental matches. A more targeted fix — treating an explicit `Location: `path`` `
mention as a distinct, higher-confidence signal than an incidental filename match, so it can't
be crowded out by weaker matches at all — was identified but not built; noted as a concrete,
scoped next step rather than a vague "improve relevance ranking someday."

**Failure 3: raising the budget worked, and surfaced a third, unrelated failure mode.**
Re-run after the fix above, `lib/ansible/vars/manager.py` did make it into the prompt (~115K
tokens now used, confirming the wider budget). The response still failed — but this time
`git apply` reported *corrupt patch*, and the raw response, on inspection, simply **stopped**
1132 characters in, mid-docstring, with no closing fence and an obviously unfinished hunk.
That's far short of the 8192-token output cap, so it wasn't our limit. The Gemini API exposes
exactly this situation via `finish_reason` on the response, and one of its documented values
is `RECITATION` — generation cut short mid-response when output too closely matches known
training content. That is a real, not theoretical, risk for this exact task: the model is
being asked to reproduce real, well-known open-source code (Ansible) near-verbatim as part of
writing a correct diff. `GeminiProvider` previously discarded `finish_reason` entirely and
returned whatever text came back, so a recitation-truncated response looked identical to a
deliberately short one — it surfaced downstream as a generic, much harder to diagnose "corrupt
patch," with nothing pointing at the actual cause. Now checked explicitly
(`check_gemini_finish_reason`): any non-`STOP` reason raises a clear, specific error naming
`RECITATION` and why it's the likely explanation, instead of silently returning a truncated
diff for the rest of the pipeline to fail on for an unrelated-looking reason.

**Three further mitigations, and one real tradeoff.** Failures 2 and 3 turn out to share a
cause — the model falling back on memorized code for a famous repo — so they got treated
together:

- **Tier-0 path ranking.** File ranking now distinguishes "the description names this file's
  full path verbatim" (tier 0) from "some file shares this basename" (tier 1). SWE-bench Pro's
  `interface` field spells the path out (`Location: `lib/ansible/vars/manager.py``), so tier 0
  is nearly always the file that must change. Measured against the real failing prompt: the
  target file moves from rank #37 of 1001 to **#0**, and the large unrelated
  `config/manager.py` that had starved it now sorts behind it. This is the targeted fix that
  §9's earlier draft named as a next step, now built — the byte-budget increase alone made the
  failure less likely; this makes that particular starvation structurally impossible.
- **Anti-memorization instruction.** The prompt now states outright that the checkout is at a
  pinned commit which likely differs from training data, that context lines must match the
  provided file contents character for character, and that a file not shown must not be
  guessed at. Cheap, and aimed at the actual observed behaviour rather than at diff syntax.
- **Temperature: a genuine tradeoff, resolved by scoping rather than by picking a side.**
  Temperature 0 maximizes reproducibility — which this harness is explicitly built around —
  but it also maximizes the chance of emitting
  *verbatim* memorized text, which is exactly what trips the recitation filter. Raising the
  default to buy recitation-resistance would trade a certain, always-paid cost (non-determinism
  on every run) for a speculative, rarely-needed benefit. Instead the first attempt stays at
  0.0 and a **single retry at 0.2 happens only when RECITATION actually occurred** — normal
  runs remain reproducible, the pathological case gets the entropy it needs. Being honest
  about the limit: that resampling defeats a recitation block is inferred from how such filters
  work, not documented or guaranteed by Google; if the retry also fails, the error surfaces
  clearly rather than being hidden.

One smaller correctness note from reviewing the finish-reason check: the first version emitted
a single error message listing *every* possible cause, which would have told a user "output
matched training content" for what was really a token-cap stop. Errors are now looked up per
reason, with an explicit generic fallback for values the harness doesn't know — the enum
already has seventeen values and gains more over time, so an unrecognised reason must fail
loudly without being misattributed to recitation.

**The actual root cause, found last: the model could not see the code it was asked to fix.**
Every mitigation above was real, but they were all treating symptoms. `read_many()` in
`runner.py` capped every file at 20,000 bytes with `head -c` — *inside the container*, at read
time. The code needing the fix lives at line 786 of a ~35KB file, far past the cut. So the
model was handed a file that stopped mid-function, with nothing marking it partial, and asked
to repair code that simply was not there. Reconstructing the region from training memory was
the only move available to it, and every observed symptom follows from that: the hallucinated
import, the context lines matching no real line, and finally — once ranking and budgets were
fixed but truncation was not — a diff that *applied cleanly* by defining a **brand-new
`VarsWithSources` class at line 60**, duplicating the one already at line 786. Python takes
the later definition, so the original unfixed class won and every test outcome was identical
to baseline. A cleanly-applied patch that changes nothing is the most misleading failure of
the three.

Two things made this hard to see, both worth recording as process lessons rather than code
notes. First, there were **two separate `MAX_FILE_BYTES` constants**, one in `runner.py` and
one in `solvers.py`. Raising the solver's cap looked like a fix, changed nothing, and produced
a prompt still showing exactly 20,042 bytes — the constant that mattered was the other one.
Second, the truncation was *silent by construction*: `head -c N` returns exactly N bytes for
both an N-byte file and a 10MB one, so nothing downstream could tell a complete file from a
severed one.

The fix is a principle rather than a bigger number: **a file is included whole or not at all.**
An absent file is honest — the model cannot use what it was not given, and can say so. A
truncated file is a trap, because it looks complete, which makes guessing look reasonable.
Files that genuinely cannot fit are now named under a `## Files NOT shown` heading so the model
knows they exist but were withheld. `read_many()` takes the cap from its caller instead of
imposing a hidden one, because truncating at the source is invisible to the only code that
knows how much it needs. The per-file cap it replaced was inherited from an era of 8K–32K
context windows; against Gemini 2.5 Flash's 1M-token window the file in question is ~9K
tokens. The constraint had stopped being real long before it started causing this bug, and it
should have been deleted rather than tuned — twice.

**Result:** `ansible-vars-001` — a real SWE-bench Pro instance, 15 pass2pass + 1 fail2pass —
now reaches `SOLVED` with `gemini-2.5-flash`. The model touched exactly one file, added the
three methods to the *existing* class, and used the file's real API (`self.data`,
`self.sources`, `VarsWithSources.new_vars_with_sources()`) — none of which appears in its
earlier from-memory attempts, which is the clearest evidence it was finally reading the
provided code rather than recalling it.

### What a hard task looks like when the harness is working

`vuls-redhat-001` (Go) is the instance that stopped yielding to fixes, and it is worth
recording *because* it never reached `SOLVED` reliably — 1 success in 5 attempts across two
models. Three attempts produced three genuinely different wrong fixes, and the harness
caught each one for a different reason:

| Attempt | What the model did | How it was caught |
|---|---|---|
| `flash-lite` | Prefixed the epoch inside `splitFileName`, not noticing the **caller already does it** → `1:1:9-123a` where `1:9-123a` was specified | test assertion |
| `flash-lite` | Fixed **one** of two byte-identical call sites; the other still returned an error | test assertion |
| `2.5-flash` | Fixed both sites but wrote `o.log.Warnf("... %w", err)` — `%w` is `fmt.Errorf`-only, so **the package did not compile** | **missing-test detection** |

That last row is the one that matters most for the harness. A build failure means `go test`
emits *no results at all*, so `all(outcome == "passed")` over an empty set is vacuously true —
a patch that breaks the build would have scored **`SOLVED`**. The `_selected_tests.txt`
manifest check (§5.4, adopted from Multi-SWE-bench) is what turns that into `REGRESSION`
instead, and this is the first time it caught a real one rather than a synthetic test.

Two findings from trying to fix this by tuning, both negative results worth keeping:

**More reasoning did not help.** Raising `thinking_level` from LOW to HIGH took reasoning from
7,145 to 62,911 tokens — 8.8x, 96% of the entire output budget, under 1,000 tokens of headroom
before `MAX_TOKENS` — and produced *exactly the same two defects*. The default is now MEDIUM,
chosen from that measurement rather than from intuition. It is a useful reminder that "give the
model more room to think" is a hypothesis, not a fix, and is cheap to actually test.

**The dataset's own metadata can mislead the model.** This instance's `interface` field reads
"No new interfaces are introduced." — but the golden fix changes `splitFileName` from four
return values to six. That text is in the prompt, and both models kept the four-value signature
and hacked the epoch inside the helper, which is precisely the defect. The field is defensible
if read as "no new *exported* interfaces" (`splitFileName` is unexported in Go), but a model
reads it plainly. Nothing was special-cased to work around it: the harness's job is to present
the task as the dataset defines it, and quietly editing a benchmark's problem statement to make
a model succeed would invalidate the measurement. Recorded as a property of the data.

The honest conclusion is that this is a capability ceiling, not a harness defect. A harness that
reported `SOLVED` for a patch which double-applies an epoch, or which does not compile, would be
worthless — catching those *is* the product. `ansible-vars-001` reaching `SOLVED` 4/4 on the same
model is the control that shows the pipeline itself is sound.

The deeper structural point still stands, and is why §10 remains the honest next step: **a
one-shot solver composes its diff blind.** It cannot check its own output against the real
file, cannot ask to see more, and cannot notice that what it wrote does not match. Whole-file
context removes the specific gap that caused this failure; it does not give the model the
ability to verify. Every fix in this section reduces the odds of a bad diff. None of them
changes the fact that the model is guessing at bytes it cannot re-read.

---

## 10. The agentic evolution path, sourced against real practice

The one-shot design — one prompt, one response, the harness applies whatever comes back — was
never meant to be the end state; it was the simpler thing to build first, with the
agentic version explicitly named as the next step from early on (see §13, written
before either failure in §9 above occurred). Both failures in §9 are, at root, consequences of
that choice: a model with no way to verify what it's writing against the real file will
occasionally get the format or the content wrong, with nothing to catch it before the harness
does.

Checked this against how the two reference agentic harnesses actually built it, rather than
assume: **SWE-agent**'s `Agent` class calls the LLM in the host process; the resulting action
executes inside the Docker container via a shell session, communicated through a deployment
layer (SWE-ReX) that talks to a server running in the container. **OpenHands** is architected
the same way — the backend/controller (host side) is what calls the LLM; an `ActionExecutor`
server runs *inside* the sandboxed container, receiving action requests over a REST API and
reporting results back. Neither system runs model-directed code on the host, and neither
requires the sandbox to hold LLM credentials or reach the model provider directly — the host
holds the key and decides *what* to do; the container is *where* it happens.

That is exactly the shape this harness would need, and most of the plumbing already exists for
a different reason: `ContainerFiles` in `runner.py` already reads files via `docker exec`
without ever landing repo bytes on the host disk (§6). An agent loop would add a
`docker exec`-based *write* action alongside the reads that already exist, and — this is the
part worth being precise about, because it's the genuine architectural payoff — it would let
the harness stop asking the model to *author diff text* at all. Once the agent finishes
editing real files in the container, the existing capture mechanism (`/baseline` snapshot vs.
`/current`, `git diff --no-index` — built for the current one-shot solver, for an unrelated
reason) computes the diff from real file content. Both §9 failures are specifically failures
of an LLM hand-writing diff text from partial context; an agent that edits real files and gets
its diff computed by `git` cannot produce a malformed or content-mismatched diff, by
construction.

What this would actually cost: a real multi-turn tool loop (several LLM calls per solve, not
one — more cost and latency per run), and new host-side action-execution plumbing (read/write/
search actions dispatched over `docker exec`, plus a termination signal from the model).
Deliberately not built yet — it's a materially larger change than either fix in §9, and a
half-finished agent loop would be worse than a working, now-hardened one-shot solver. Recorded here as the concrete, sourced next step, not a vague
aspiration: the shape is known, the reference implementations exist, and the harness already
has the two pieces (container-side file access, diff-by-git-not-by-LLM) that make it a smaller
lift here than it would be starting from nothing.

---

## 11. The prompt is a component, and it was engineered like one

The solver prompt ended up being the part of this system with the widest gap between "seems
obviously right" and "is actually right", so it was treated as a component with a changelog
rather than as prose to be tweaked. Four things are worth recording.

**The edit contract was reversed, and the reversal is the lesson.** An earlier version asked
the model for SEARCH/REPLACE blocks and computed the diff with `difflib`, on the reasoning
that authoring a unified diff asks a model to do three things it is bad at. That reasoning was
correct; the conclusion was not. Parsing a bespoke block format made the *harness* the
almost-right component: five self-inflicted parser bugs in short order, each rejecting model
output that was, on inspection, correct. The failure had been moved to the worse side — a
rejection now meant our parser disagreed, with no external authority to appeal to. The
governing principle, learned expensively: **prefer the contract where a failure is
unambiguously attributable to the subject under test, not to the harness.** `git apply` is
that authority. Aider reached the same place by measurement, scoring GPT-4 Turbo at 20% with
SEARCH/REPLACE and 61% with unified diffs.

**Leniency belongs in the applier, not the prompt.** Aider's published advice is to avoid
"brittle specifiers like line numbers or line counts", so the prompt was changed to stop
demanding correct `@@` counts. That was half a change: aider can afford it because their
applier does fuzzy search/replace, while ours is `git apply`, which is exact. Worse, the
counting requirement turned out to be load-bearing for an unrelated reason — a model that must
count the lines in its hunk has to *look* at every line, including the blank ones it otherwise
drops. The demand went back into the prompt and the tolerance moved into `_apply_diff` as a
bounded ladder ending in `patch --batch --fuzz=5`, which is what SWE-bench's own harness falls
back to. Leniency you can enumerate and announce is safer than leniency you hope the model
won't need.

**Position matters as much as content.** A real prompt here is ~350 KB, so every instruction
at the top sits roughly 86,000 tokens before the point where generation begins. Rules that
were violated in practice were all present up there and read past. The verification checklist
was therefore moved to *after* the file contents — last thing before the model acts — and the
prompt was audited for contradictions, which found a duplicated SCOPE block whose first copy
said "almost always this means editing ONE file" and whose second copy existed specifically to
correct that. The golden patch for the Go bundle changes two functions.

**The benchmark documents what its own fields are for, and saying so is free.** SWE-bench Pro
is not raw GitHub issues: Scale's annotators rewrite each task into a problem statement plus a
Requirements list holding "expected behavior ... that will be explicitly tested for", plus an
optional Interface section added to "mitigate false negatives for unit test verification".
Those sections are the closest thing to grading criteria that exists outside the hidden tests,
and the prompt now says so — which leaks nothing, since it points at text the model already
has. The same paper's failure taxonomy also justified weighting the compiler self-check
first: Syntax Error accounts for 56.5% of Gemini 2.5 Pro's failed instances and 31.3% of
Claude Opus 4.1's, and in a compiled language that is not partial credit — nothing builds, so
nothing runs, and the attempt scores zero.

The caveat on all of it is §7: with one sample per variant these changes cannot be
individually credited. They are defended by mechanism — a removed contradiction, a rule the
model can act on, a tolerance that cannot change a patch's meaning — not by a verdict delta.

---

## 12. The six design questions, answered directly

### Arbitrariness — how does this hold up for a language that isn't Python?

By having exactly one language-specific contract: **the harness never parses stdout, only
JUnit XML.** That is the single decision that makes Python / Go / JavaScript / Java a
`task.json` change rather than a code change — `base_image`, `setup_cmd`, `deps_cmd`,
`test_cmd`, and nothing else.

Two consequences had to be designed for rather than assumed. First, tests are restored **in
place**, at their original repo-relative paths, because Go and Java tests must sit in their
package directory to compile and JS tests with relative `require('../src')` break when moved —
so bucket separation happens at the *results* level, not the execution level. Second,
`test_cmd` needs `{dirs}` as well as `{path}`, because `go test` takes packages, not files.
Both were found by actually building the Go bundle; neither was visible from the Python one.

Proven, not claimed: `vuls-redhat-001` is a real SWE-bench Pro **Go** instance that validates,
oracles to `SOLVED`, and stubs to `UNSOLVED`. Test-id normalisation covers all three runner
conventions (pytest `::`, jest `|`, Go subtest `/` — the last of which must *not* be split,
since `/` there is a subtest separator and not a path).

### Observability — how would you debug a failed run?

Three levels, and the design intent is that you almost never need the third:

1. `evals history` — every invocation of every command, including failures, `init`, and
   `scaffold`. One row each, with verdict or status.
2. `reports/<task>-<command>.json` and the ledger's `results_json` — per-test outcomes, both
   buckets, missing tests, the solver's metadata.
3. `artifacts/run-N/` under `--keep-artifacts` — the prompt, the raw model response, the raw
   diff, the captured diff, the JUnit XML, and the test log.

`status` and `verdict` are separate columns on purpose: conflating them makes "did this crash?"
unanswerable for a run that produced a verdict. Solver metadata records what actually *varied*
between two runs — provider, model, temperature, effective reasoning level, token usage split
into prompt/thinking/output, whether the provider retried, files inlined, symbol matches —
because that is always the first question. The prompt and raw response are captured **before**
the provider call returns, so a rejected key, a 429, or an exhausted output budget still leaves
the evidence behind; a failed solve is exactly when the raw response matters most.

In practice this worked: every LLM failure in §9 and §14 was diagnosed from
artifacts in minutes, including a Go segfault traced to a nil logger on a zero-value receiver.

### Artifacts & debuggability — what do you keep, and why that set?

The **diff is the durable artifact**, and it is written to the ledger at the moment it exists —
before grading — because it is the only expensive, non-reproducible thing a run produces.
Everything else can be recomputed from it. That single checkpoint is what makes `evals resume`
(finish a run that died during grading) and `evals replay` (re-grade as a new run, linked by
`source_run_id`) possible without paying for inference again.

Bulk material is deliberately kept **out** of the database and on disk instead: a real prompt
is ~350 KB, and inlining that into a SQLite row would make the ledger unusable for the queries
it exists to serve. The split is enforced in both the success and failure paths.

This is also what made iterating safe. After each harness fix, stored diffs were replayed
rather than regenerated — which is both free and, per §7, the only way to actually re-examine
the same input.

### Isolation — what stops a solver from cheating or escaping?

Each side gets exactly one dangerous capability and never both.

The **solve** container can execute arbitrary model-authored code, but has `--network none`,
runs as non-root `appuser`, is capped at 2 GB, never holds the API key (the host makes the LLM
call), and — critically — the graded tests are *absent from its filesystem*, `rm -rf`'d at
image build time rather than hidden behind a permission or masked by a mount.

The **grade** container has the tests, but the code it runs was frozen into a diff first, is
re-applied in a *fresh* container, and has `protected_paths` (conftest.py, pytest.ini, …) reset
from git afterwards — so a patch that edits test infrastructure has that edit undone before
anything executes. Hiding tests is necessary but not sufficient: a solver could otherwise
influence grading through a root `conftest.py` without ever seeing a test.

This was verified adversarially, not assumed: an identical cheating `conftest.py` produces
`SOLVED` with the reset disabled and `UNSOLVED` with it enabled.

One deliberate limit, stated rather than hidden: containers are the boundary, and container
escape is out of scope. A hostile solver is not the threat model — an *optimising* one is.

### Reproducibility — what is guaranteed, and what isn't?

Guaranteed: the commit SHA, the image tag (`evals/<task_id>:<commit>`), the baseline
snapshot frozen after `deps_cmd`, bucket attribution, and the verdict function. `HEAD` inside
the image always equals the declared SHA, and the solver's changes are captured by diffing a
fresh copy against `/baseline` rather than against git history — so `HEAD` never moves and no
synthetic commits are created.

Not guaranteed, and documented rather than papered over: `deps_cmd` resolving floating
dependency versions over time (the bundle author's choice — pin them), and **the model
itself**. As §7 sets out with measurements, `temperature=0` does not deliver determinism; two
byte-identical prompts produced different patches and different verdicts. The harness's
reproducibility claim is therefore scoped precisely: *everything downstream of the model is
deterministic*, and `replay` is the mechanism that exploits it.

### Performance — where does the time go, and what would you do about it?

Dominated by two things, neither of which is harness overhead: the image build on first `init`
(minutes, cached thereafter — `validate`/`run` start in about a second), and the test run
itself, where a compiled language pays a build cost inside a single `docker exec` (`go test`
compiles before running anything, at one point pegging 19 cores while looking, from the
outside, exactly like a hang — which is why `--verbose` exists).

Cheap wins already taken: fetching only the pinned commit rather than cloning full history
(gigabytes on ansible/teleport, and it would land in every image *and* every `/baseline` copy);
one batched `docker exec` to read candidate files rather than one per file; `grep` run *inside*
the container returning only paths, so ranking costs one exec and a few hundred bytes.

Parallelism is supported by the design and not yet exercised in anger: container names are
UUID-suffixed, image tags are per-task, there is no shared mutable state, and SQLite runs in
WAL mode so a second terminal can read while a run writes. The real constraints are memory
(2 GB × N) and per-run report paths, not correctness.

The biggest available win is not a speed-up but a cost avoidance, and it already exists:
`replay` re-grades a stored diff for free. Re-running a harness change across every historical
run costs nothing in inference.

---

## 13. Alternatives considered and rejected

### Ghost volumes — masking hidden tests with an empty bind mount

*Leave the tests in the image; bind-mount an empty directory over the test folder during
solve; omit the mount during grade so they "reappear."*

Rejected, four reasons, the first fatal:

1. **fail2pass tests usually don't exist in the base repo at all.** In SWE-bench, `test_patch`
   *is* the diff that adds them — there is nothing in the image to unmask. Masking would only
   handle pre-existing pass2pass tests, so you'd still need injection for fail2pass and end up
   maintaining **both** mechanisms.
2. **It hides the whole directory.** The model needs to see all *other* tests.
   An empty mount over `tests/` hides those too.
3. **Runtime control instead of a build-time guarantee.** Masking leaves the answers inside
   the image: anyone who runs it without the flag sees them, and any future code path that
   forgets the flag leaks them. Stripping at build makes leaking structurally impossible.
4. **The performance argument targets the wrong copy.** Restoring tests is a few small files;
   the genuinely heavy operation was bulk repo extraction, which volumes don't fix either.

**Chosen:** strip at build via `hidden_paths`, restore in place at grade time via a tar stream.

### `git diff HEAD` to capture the solver's patch

Rejected. `init` deletes `hidden_paths`, so the tree is already dirty relative to `HEAD` and
`git diff` would fold that housekeeping deletion into the solver's patch — which would then
be re-applied in the grading container and delete the very tests about to grade it.
Committing the stripped state fixes that but moves `HEAD` off the pinned commit, breaking
the reproducibility anchor.

**Chosen:** freeze `/baseline` at build time (after deps), capture with
`git diff --no-index /baseline /current`, and rewrite the headers back to plain `a/` `b/`
form so the stored artifact is an ordinary patch that applies with `-p1`.

### Solver inside the task container, API key passed with `-e`

Rejected for now, and documented as the agentic evolution path.

`-e` is the correct *mechanism* for giving a container a secret. The problem is *which*
container — this is the designated place where untrusted code runs. Today's one-shot solver
executes no repo code in container A, so the risk is near zero. But the A/B split exists
precisely to support a future agentic solver, and then model-directed commands would run in
a container holding your key with the network enabled. Prompt injection from a malicious
repo to exfiltration becomes one step.

It also breaks language-agnosticism: every task image, in every language, would need Python
and a provider SDK just to make one HTTPS call.

**Evolution path:** a reverse-proxy sidecar. The container routes LLM requests through a
proxy that injects the key and forwards only to allowed provider endpoints, so it never
holds the credential and never reaches the open internet.

### Hand-rolling the container execution layer

Worth stating plainly, because it is the alternative most likely to be raised: `docker_mgr.py`
(191 lines) plus `ContainerFiles` is a minimal reimplementation of a slice of
[SWE-ReX](https://github.com/SWE-agent/SWE-ReX), the runtime layer under SWE-agent. SWE-ReX
gives one interface for running commands in a sandbox — local, Docker, AWS Fargate or Modal —
with the agent code unchanged across all of them: a FastAPI server inside the sandbox,
interactive shell support, parallel sessions, and completion detection by appending a
sentinel to each command and watching stdout for it. OpenHands' runtime is the same shape
with an event-stream architecture, and Modal/E2B/Daytona sell the abstraction as a service.

**Not adopted, deliberately.** This harness needs exactly three container operations: read
files, apply a diff, run a test command. A subprocess wrapper over the docker CLI is
auditable in a single sitting, adds no dependency, and needs no server process inside the
task image — which matters when the image is a third-party benchmark artifact we do not
control.

**Where that stops being true:** the moment a solver needs an interactive shell, several
sessions in parallel, reliable "has this command finished?" detection, or a cloud backend for
horizontal scale. All four arrive together with the agentic loop of §10. At that point the
correct move is to adopt SWE-ReX rather than grow `docker_mgr.py` toward it — the failure
mode to avoid is reimplementing a mature execution layer badly, one requirement at a time.

### A full event-sourced state machine

*`INITIALIZED → BASELINE_VALIDATED → SOLVING → PATCH_CAPTURED → GRADING → EVALUATED`, with
resumption from any state.*

Partially adopted. The resumption *concept* is right. But only **one** state is a meaningful
checkpoint — `PATCH_CAPTURED`. Everything before it is cheap to redo and everything after it
is cheap to redo. Six states would be ceremony around a single durable write.

**Chosen:** one lifecycle column, with `PATCH_CAPTURED` as the resume point.

### Log parsing instead of JUnit XML

This is the one place the harness knowingly diverges from SWE-bench, and the research
supports the divergence.

SWE-bench parses **stdout**, delimited by `START_TEST_OUTPUT` / `END_TEST_OUTPUT`, using
per-repo and per-framework parsers. Multi-SWE-bench (8 languages) extends this with
"adaptive log parsers — including LLM-generated scripts and known regex frameworks", falling
back to **LLM code generation when regex parsing fails**. SWE-Bench++ uses "constrained
neural synthesis to generate adaptive log parsers across 11 languages."

That is strong evidence that log parsing does not scale gracefully. Multi-SWE-bench reports
a concrete failure: in some Java projects, "test outputs from concurrent threads are
interleaved without delimiters, making rule-based log parsing infeasible."

| | Log parsing (SWE-bench) | JUnit XML (ours) |
|---|---|---|
| Parsers to maintain | one per framework, sometimes per repo | **one, forever** |
| Extra deps in image | none | a reporter (pytest: built in; `jest-junit`; `go-junit-report`; Surefire: built in) |
| Concurrent/interleaved output | **breaks** — documented Java failure | unaffected, it's a structured file |
| Test ids, timings, messages | must be regexed out | free |
| When no reporter can be installed | works | needs a bundle-supplied wrapper |

**Why we can afford XML where SWE-bench can't:** they ingest thousands of pre-existing repos
and cannot assume anything about tooling. We control the image through `setup_cmd`, so
installing a reporter is one line — and the Java case that defeats their parser is precisely
where Surefire's native XML is easiest.

### Bulk repo extraction to the host

Rejected and removed. No reference system does this — SWE-agent and OpenHands read files by
executing commands in the container and shipping back only what's needed. Tarring the whole
tree out is what put the repo on the host disk in the first place, and it caused a Windows
symlink bug. The prompt only ever inlines a fraction of it.

**Chosen:** targeted `docker exec` reads. This *deleted* code — the tar machinery, symlink
filtering, the tar-slip guard, and `filter="data"` handling all existed only because we
extracted.

---

## 14. What a hard instance looks like when the harness is working

`vuls-redhat-001` is a real SWE-bench Pro Go instance that the LLM does **not** solve. That
is a legitimate result to report, and worth recording in detail because it is the case the
harness exists to illuminate.

Three Gemini models, all producing patches that applied cleanly:

| model | patch applied | verdict | root cause |
|---|:---:|---|---|
| 3.1-flash-lite | yes | UNSOLVED → PARTIAL | epoch applied twice; nil-receiver panic |
| 2.5-flash | yes | REGRESSION | `undefined: relIndex` — a half-finished rename |
| 3-flash-preview | yes | REGRESSION | `declared and not used: err` |

Every patch applied, so the diff pipeline, workdir resolution and capture are not implicated.
Two of three simply did not compile.

That distribution is not unusual — it matches the benchmark's own published analysis. Scale's
SWE-bench Pro paper reports **Syntax Error at 56.5% of Gemini 2.5 Pro's failed instances**
and 31.3% of Claude Opus 4.1's, with Wrong Solution at 50.3% for Opus. The harness reproduced
the benchmark's known failure modes.

The missing-test detection (§4) is what turned a Go compile error into a correct `REGRESSION`
rather than a false pass: with zero tests executed, `all(passed)` over an empty list is
`True`, so without that check a patch that does not build would have scored pass2pass green.

The fix is not a better prompt. Every failure above would be caught by a single compiler run,
which is what §10's feedback loop provides.

---

### References

- [SWE-bench harness](https://www.swebench.com/SWE-bench/reference/harness/) ·
  [eval script generation](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/test_spec/python.py)
- [SWE-agent architecture](https://swe-agent.com/latest/background/architecture/) ·
  [SWE-ReX (the sandboxed execution layer SWE-agent runs actions through)](https://github.com/SWE-agent/SWE-ReX)
- [OpenHands runtime architecture](https://docs.openhands.dev/openhands/usage/architecture/runtime)
- [Multi-SWE-bench](https://arxiv.org/html/2504.02605v1) ·
  [SWE-Bench++](https://arxiv.org/html/2512.17419v1)
- [SWE-bench Pro dataset](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) ·
  [paper (failure taxonomy, Requirements/Interface rationale)](https://arxiv.org/html/2509.16941v2) ·
  [open-source harness](https://github.com/scaleapi/SWE-bench_Pro-os)
- [aider — unified diffs make GPT-4 Turbo 3X less lazy](https://aider.chat/docs/unified-diffs.html)
- [SWE-agent default config (system/instance templates)](https://github.com/princeton-nlp/SWE-agent/blob/main/config/default.yaml)
