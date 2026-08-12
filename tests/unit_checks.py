"""Fast, dependency-free unit checks for the parts of the harness that are pure logic.

Run with:  python tests/unit_checks.py

These cover the pieces that are easy to break and expensive to catch in a real container
run: ledger migration, diff normalization, protected-path matching, bucket attribution,
and the language-profile contract. Anything needing Docker lives in tests/linux/ instead.
"""
import json
import re
import sqlite3
import tempfile
from pathlib import Path

from agent_evals.bundle import BundleError, TaskBundle
from agent_evals.db import RunDB, STATUS_PATCH_CAPTURED, STATUS_SUCCESS
from agent_evals.runner import (
    normalize_captured_diff, diff_touched_paths, _is_protected, expected_test_names,
    manifest_test_key, parse_junit_xml, render_bucket_table,
)
from agent_evals.cli import _load_dotenv
from agent_evals.solvers import LLMSolver, check_gemini_finish_reason
from agent_evals.scaffold import (
    LANGUAGE_PROFILES, _looks_like_a_path, guardrail_paths_from_setup_cmd, resolve_test_files,
)

tmp = Path(tempfile.mkdtemp())

# --------------------------------------------------------------- 1. migration
old_db = tmp / "old_runs.db"
conn = sqlite3.connect(old_db)
conn.execute("""CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, command TEXT NOT NULL,
    task_id TEXT, args_json TEXT NOT NULL, status TEXT NOT NULL,
    results_json TEXT, log TEXT)""")
conn.execute(
    "INSERT INTO runs (ts, command, task_id, args_json, status, log) VALUES (?,?,?,?,?,?)",
    ("2026-01-01T00:00:00+00:00", "run", "bundles/old", json.dumps({"bundle": "bundles/old"}),
     "SOLVED", "legacy row"),
)
conn.commit()
conn.close()

with RunDB(old_db) as db:
    rows = db.list_runs()
    assert len(rows) == 1, rows
    assert rows[0]["log"] == "legacy row"
    assert rows[0]["created_at"] == "2026-01-01T00:00:00+00:00", rows[0]
    # New columns exist and are writable on the migrated table
    db.update_run(1, captured_diff="diff --git a/x b/x\n", status=STATUS_PATCH_CAPTURED)
    assert db.get_run(1)["captured_diff"].startswith("diff --git")
print("OK migration: legacy rows preserved, timestamps backfilled, new columns writable")

# --------------------------------------------------------------- 2. allowlist
with RunDB(tmp / "new.db") as db:
    rid = db.create_run("run", "b", {"bundle": "b"})
    try:
        db.update_run(rid, **{"status; DROP TABLE runs--": "x"})
        raise AssertionError("expected ValueError for non-updatable column")
    except ValueError as e:
        assert "non-updatable" in str(e)
    db.update_run(rid, results={"a": 1})
    assert db.get_run(rid)["results"] == {"a": 1}
    db.finish_run(rid, status=STATUS_SUCCESS, verdict="SOLVED")
    assert db.get_run(rid)["verdict"] == "SOLVED"
    assert db.get_run(rid)["status"] == STATUS_SUCCESS
print("OK ledger: column allowlist rejects injection, status/verdict independent")

# --------------------------------------------------------------- 3. diff normalization
raw = (
    "diff --git a/baseline/calc/calc.py b/current/calc/calc.py\n"
    "index c064e26..dd83ea9 100644\n"
    "--- a/baseline/calc/calc.py\n"
    "+++ b/current/calc/calc.py\n"
    "@@ -3,4 +3,6 @@ def add(a, b):\n"
    " \n"
    "+    if b == 0:\n"
    "     return a / b\n"
)
norm = normalize_captured_diff(raw)
assert "a/baseline/" not in norm and "b/current/" not in norm, norm
assert "--- a/calc/calc.py" in norm and "+++ b/calc/calc.py" in norm
assert "diff --git a/calc/calc.py b/calc/calc.py" in norm
# content lines untouched
assert "+    if b == 0:" in norm
print("OK diff normalization -> plain a/ b/ (applies with -p1)")

# new-file case with /dev/null
newfile = (
    "diff --git a/baseline/new.py b/current/new.py\n"
    "new file mode 100644\n--- /dev/null\n+++ b/current/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
)
n2 = normalize_captured_diff(newfile)
assert "--- /dev/null" in n2 and "+++ b/new.py" in n2, n2
print("OK diff normalization handles /dev/null new files")

# --------------------------------------------------------------- 4. touched paths
assert diff_touched_paths(norm) == ["calc/calc.py"]
conftest_diff = "--- a/conftest.py\n+++ b/conftest.py\n"
assert diff_touched_paths(conftest_diff) == ["conftest.py"]
print("OK diff_touched_paths")

# --------------------------------------------------------------- 5. protected matching
prot = ("*conftest.py", "*pytest.ini", "*tox.ini", "*sitecustomize.py")
assert _is_protected("conftest.py", prot)
assert _is_protected("tests/unit/conftest.py", prot)
assert _is_protected("pytest.ini", prot)
assert _is_protected("src/sitecustomize.py", prot)
assert not _is_protected("calc/calc.py", prot)
assert not _is_protected("src/config.py", prot)
print("OK protected-path matching (nested + root, no false positives)")

# --------------------------------------------------------------- 6. missing tests
bucket = tmp / "p2p"
bucket.mkdir()
(bucket / "_selected_tests.txt").write_text(
    "test_vars.py::TestX::test_a\ntest_vars.py::TestX::test_b\n", encoding="utf-8")
assert expected_test_names(bucket) == {"test_a", "test_b"}
assert expected_test_names(tmp / "nonexistent") == set()
print("OK expected_test_names (and graceful skip for hand-authored bundles)")

xml = tmp / "r.xml"
xml.write_text(
    '<testsuites><testsuite tests="1">'
    '<testcase classname="pkg.test_vars.TestX" name="test_a"/>'
    "</testsuite></testsuites>", encoding="utf-8")
cases = parse_junit_xml(xml)
observed = {c["name"] for c in cases}
missing = sorted(expected_test_names(bucket) - observed)
assert missing == ["test_b"], missing
print("OK missing-test detection: a vanished test is caught (was silently OK before)")

# the regression this closes: all *present* tests passed, yet the bucket must FAIL
buckets = {"pass2pass": {"ok": False, "tests": cases, "missing_tests": missing, "exit_code": 0}}
table = render_bucket_table(buckets, {"pass2pass": "passed"})
assert "MISSING" in table and "1 MISSING" in table, table
print("OK table surfaces MISSING rows")

# --------------------------------------------------------------- 7. XML guards
bomb = tmp / "bomb.xml"
bomb.write_text('<!DOCTYPE t [<!ENTITY a "x">]><testsuites/>', encoding="utf-8")
try:
    parse_junit_xml(bomb)
    raise AssertionError("expected entity rejection")
except RuntimeError as e:
    assert "entit" in str(e).lower()
print("OK XML entity-expansion guard")

# --------------------------------------------------------------- 8. id -> reporter name
# Each runner writes benchmark test ids differently; bucket attribution compares them
# against the JUnit `name` attribute, so the reduction has to be right per runner.
assert manifest_test_key("test/units/utils/test_vars.py::TestVars::test_x") == "test_x"
assert manifest_test_key("tests/test_add.py::test_add") == "test_add"
# Go: `/` is the SUBTEST separator, not a path - splitting on it would corrupt the name.
assert manifest_test_key("TestHTTPConnStateReporter/without_client_certs") \
    == "TestHTTPConnStateReporter/without_client_certs"
assert manifest_test_key("TestHTTPConnStateReporter") == "TestHTTPConnStateReporter"
# jest-junit's default titleTemplate is "{classname} {title}": ancestor describe titles
# space-joined then the test title. The leading segment is the file, which jest omits.
assert manifest_test_key("test/database.js | Test database | should work") \
    == "Test database should work"
assert manifest_test_key("src/app/helpers/elements.test.ts | isFromProton should be an element from Proton") \
    == "isFromProton should be an element from Proton"
print("OK manifest_test_key: pytest / go subtest / jest id shapes")

# --------------------------------------------------------------- 9. test-file resolution
assert _looks_like_a_path("test/units/utils/test_vars.py::TestVars::test_x")
assert not _looks_like_a_path("TestHTTPConnStateReporter/without_client_certs")
assert not _looks_like_a_path("test/database.js | Test database should work")
before = (
    "git reset --hard abc\ngit clean -fd \ngit checkout abc \n"
    "git checkout def -- lib/srv/ingress/reporter_test.go\n"
)
assert guardrail_paths_from_setup_cmd(before) == ["lib/srv/ingress/reporter_test.go"]
# Go ids carry no path, so resolution must fall back to before_repo_set_cmd.
assert resolve_test_files(["TestHTTPConnStateReporter"], before, "go") \
    == ["lib/srv/ingress/reporter_test.go"]
# ...and with neither source available, fail loudly rather than guess.
try:
    resolve_test_files(["TestX"], "", "go")
    raise AssertionError("expected BundleError when no path source exists")
except BundleError as e:
    assert "Write this bundle by hand" in str(e)
print("OK test-file resolution: ids when they carry a path, before_repo_set_cmd otherwise")

# --------------------------------------------------------------- 10. language profiles
for lang, profile in LANGUAGE_PROFILES.items():
    cmd = profile["test_cmd"]
    assert "{path}" in cmd or "{dirs}" in cmd, f"{lang}: test_cmd names no test target"
    # Only pytest gets --junitxml appended for it; everything else must say where the
    # report goes, or the runner will find no XML to read.
    if not cmd.startswith("pytest"):
        assert "{report}" in cmd, f"{lang}: non-pytest test_cmd must use {{report}}"
    assert "git" in profile["setup_cmd"], f"{lang}: setup_cmd must install git"
    assert profile["reporter_cmd"] in profile["setup_cmd"], f"{lang}: reporter not installed"
# Go takes packages, not files - a .go test file cannot be compiled on its own.
assert "{dirs}" in LANGUAGE_PROFILES["go"]["test_cmd"]
assert "{path}" in LANGUAGE_PROFILES["js"]["test_cmd"]
print("OK language profiles: every test_cmd names a target and a JUnit destination")

# --------------------------------------------------------------- 11. task.json contract
def _write_bundle(dirname: str, test_cmd: str) -> Path:
    root = tmp / dirname
    (root / "tests" / "pass2pass").mkdir(parents=True)
    (root / "tests" / "fail2pass").mkdir(parents=True)
    (root / "description.md").write_text("d", encoding="utf-8")
    (root / "patch.diff").write_text("", encoding="utf-8")
    (root / "task.json").write_text(json.dumps({
        "task_id": dirname, "repo": "https://example.com/r.git", "commit": "abc",
        "test_cmd": test_cmd, "deps_cmd": "true",
    }), encoding="utf-8")
    return root

# {dirs} alone is enough - this is what every Go bundle uses.
assert TaskBundle(_write_bundle("go_ok", "go test -v {dirs} > {report}")).task.test_cmd
try:
    TaskBundle(_write_bundle("no_target", "pytest -q"))
    raise AssertionError("expected BundleError for a test_cmd with no test target")
except BundleError as e:
    assert "{path}" in str(e) and "{dirs}" in str(e)
# A Windows drive letter cannot exist inside the Linux container.
try:
    TaskBundle(_write_bundle("drive", "pytest {path} --rootdir C:/msys64/workspace"))
    raise AssertionError("expected BundleError for a Windows path")
except BundleError as e:
    assert "MSYS_NO_PATHCONV" in str(e)
print("OK task.json contract: {dirs} accepted, no-target rejected, MSYS path rejected")

# --------------------------------------------------------------- 12. LLM diff repair
# Reproduces a real failure: a Gemini response with a context line missing its required
# leading space, which made git apply reject the whole hunk with "corrupt patch".
gemini_style = (
    "```diff\n"
    "--- a/calc/calc.py\n"
    "+++ b/calc/calc.py\n"
    "@@ -3,4 +3,6 @@\n"
    " \n"
    "def add(a, b):\n"          # missing its leading space - the bug
    "-    return a + b\n"
    "+    if a is None:\n"
    "+        return b\n"
    "+    return a + b\n"
    "```\n"
)
repaired = LLMSolver._extract_diff(gemini_style)
assert " def add(a, b):" in repaired, repaired
assert "\ndef add(a, b):" not in repaired, repaired  # the unrepaired (unprefixed) form is gone
# Lines that were already correctly prefixed must be untouched.
assert "-    return a + b" in repaired
assert "+    if a is None:" in repaired
# A trailing blank line from a closing fence isn't itself a hunk line - must not gain a
# spurious " " that would corrupt the file's actual trailing newline.
no_fence = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n+new\n"
assert LLMSolver._extract_diff(no_fence) == no_fence
print("OK LLM diff repair: missing leading space on a hunk context line is restored")

# A fix spanning several files arrives as one multi-file diff. Repair must treat each file
# independently: the `---`/`+++` headers end the previous hunk, so a bare context line in the
# second file is still repaired and the headers themselves never get a space prepended.
multi = (
    "```diff\n"
    "--- a/pkg/one.py\n+++ b/pkg/one.py\n@@ -1,3 +1,3 @@\n def a():\n-    return 1\n+    return 2\n"
    "--- a/pkg/two.py\n+++ b/pkg/two.py\n@@ -1,3 +1,3 @@\ndef b():\n-    return 3\n+    return 4\n"
    "```\n"
)
multi_out = LLMSolver._extract_diff(multi)
assert "--- a/pkg/one.py" in multi_out and "--- a/pkg/two.py" in multi_out, multi_out
assert " def a():" in multi_out and " def b():" in multi_out, multi_out  # both repaired/kept
assert " --- a/pkg/two.py" not in multi_out, "file header must not be treated as a hunk line"
print("OK LLM diff repair: multi-file diffs keep every file and repair each independently")

# Real failure: a Gemini hunk header claimed "-10,4 +10,4" over a body with 6 old / 5 new
# lines, so git rejected the whole patch with "corrupt patch at line 300". Models count lines
# far less reliably than they write them, so the body is authoritative.
wrong_counts = (
    "--- a/req.txt\n+++ b/req.txt\n"
    "@@ -10,4 +10,4 @@\n"
    " ctx1\n ctx2\n ctx3\n-old1\n-old2\n-old3\n+new1\n+new2\n"
)
fixed = LLMSolver._extract_diff(wrong_counts)
assert "@@ -10,6 +10,5 @@" in fixed, fixed          # 3 ctx + 3 removed / 3 ctx + 2 added
assert " ctx1" in fixed and "-old1" in fixed and "+new1" in fixed, fixed  # body untouched
# Trailing context after the second @@ must be preserved (git and humans both use it).
assert "@@ -1,2 +1,2 @@ def f():" in LLMSolver._extract_diff(
    "--- a/x\n+++ b/x\n@@ -1,9 +1,9 @@ def f():\n ctx\n-a\n+b\n"
)
# Correct headers must be left exactly as they are.
already_ok = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-a\n+b\n"
assert LLMSolver._extract_diff(already_ok) == already_ok
print("OK LLM diff repair: wrong @@ line counts recomputed from the hunk body")

# A model wrapped a correct two-hunk diff in <diff>...</diff>. The opening tag was removed by
# the prose trim (it precedes the first ---), but the CLOSING tag landed inside the final hunk,
# and _repair_hunk_lines then "fixed" that unprefixed line into a context line - so git hunted
# for a literal </diff> in the source and the hunk failed. Stripping must therefore run BEFORE
# repair: a repair pass that doesn't know what it's repairing can make things worse.
_wrapped = (
    "<diff>\n--- a/x.go\n+++ b/x.go\n@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n</diff>\n"
)
_unwrapped = LLMSolver._extract_diff(_wrapped)
assert "</diff>" not in _unwrapped and "<diff>" not in _unwrapped, _unwrapped
assert _unwrapped.startswith("--- a/x.go"), _unwrapped
assert _unwrapped.rstrip().endswith("+new"), _unwrapped

# Only bare tags at column ZERO go. A real HTML diff always carries a ' ', '+' or '-' prefix,
# so its markup must survive untouched.
_html = "--- a/p.html\n+++ b/p.html\n@@ -1,3 +1,3 @@\n <div>\n-</div>\n+</section>\n"
assert LLMSolver._extract_diff(_html).count("</div>") == 1, "prefixed markup must survive"
assert "+</section>" in LLMSolver._extract_diff(_html)
print("OK LLM diff repair: XML wrapper tags stripped, real markup in hunks preserved")

# --------------------------------------------------------------- 13. .env loading
import os as _os  # local import: this is the only check that touches process env
env_file = tmp / "dotenv_test.env"
env_file.write_text(
    "# a comment\n\nFOO_TEST_KEY=bar123\nQUOTED=\"value with spaces\"\nPRESET=should_not_override\n",
    encoding="utf-8",
)
_os.environ["PRESET"] = "real_value"
_os.environ.pop("FOO_TEST_KEY", None)
_load_dotenv(env_file)
assert _os.environ["FOO_TEST_KEY"] == "bar123"
assert _os.environ["QUOTED"] == "value with spaces"
assert _os.environ["PRESET"] == "real_value"  # a real env var is never overwritten
assert _load_dotenv(tmp / "nonexistent.env") is None  # missing file is a silent no-op
print("OK .env loading: values applied, quotes stripped, real env vars take precedence")

# A shell variable left over from an earlier session silently shadowed a freshly-written .env,
# and the run failed with "credentials rejected" while the .env file looked perfectly correct.
# Precedence is fine; hiding which value won is not - the conflict must be announced.
import io as _io, contextlib as _ctx
_conf = tmp / "conflict.env"
_conf.write_text("SHADOW_TEST_KEY=from-dotenv\n", encoding="utf-8")

_os.environ["SHADOW_TEST_KEY"] = "from-shell"
_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    _load_dotenv(_conf)
assert _os.environ["SHADOW_TEST_KEY"] == "from-shell", "exported value must still win"
assert "already set in your shell" in _buf.getvalue(), "shadowing must be announced"
assert _buf.getvalue().isascii(), "message must be ASCII for cp1252 consoles"

# No conflict -> no noise. A matching value or an unset variable must stay silent.
for _pre, _label in ((None, "unset"), ("from-dotenv", "identical")):
    _os.environ.pop("SHADOW_TEST_KEY", None)
    if _pre:
        _os.environ["SHADOW_TEST_KEY"] = _pre
    _quiet = _io.StringIO()
    with _ctx.redirect_stdout(_quiet):
        _load_dotenv(_conf)
    assert _quiet.getvalue() == "", f"{_label} case must not warn: {_quiet.getvalue()!r}"
print("OK .env shadowing: conflict announced, silent when values agree or none is set")

# --------------------------------------------------------------- 14. Gemini finish_reason
# A real failure: Gemini truncated a response mid-diff (1132 chars, cut off mid-docstring)
# with no error - just silently short output that later failed as a generic "corrupt patch"
# from git apply. finish_reason is how the API actually signals this; must not be ignored.
check_gemini_finish_reason("STOP", "gemini-2.5-flash")  # normal completion: no error
check_gemini_finish_reason(None, "gemini-2.5-flash")    # some SDK versions omit it: no error
for bad in ("RECITATION", "MAX_TOKENS", "SAFETY", "FinishReason.RECITATION"):
    try:
        check_gemini_finish_reason(bad, "gemini-2.5-flash")
        raise AssertionError(f"expected RuntimeError for finish_reason={bad}")
    except RuntimeError as e:
        assert bad.split(".")[-1] in str(e)
print("OK Gemini finish_reason check: STOP/None pass, RECITATION/MAX_TOKENS/SAFETY raise")

# Each finish_reason must explain ITS OWN cause. A blanket message listing every possibility
# would tell the user "output matched training content" for what was really a token-cap stop.
for reason, expected, forbidden in (
    ("MAX_TOKENS", "output-token cap", "training content"),
    ("SAFETY", "safety filters", "training content"),
    ("RECITATION", "training content", "output cap"),
):
    try:
        check_gemini_finish_reason(reason, "m")
        raise AssertionError(f"expected raise for {reason}")
    except RuntimeError as e:
        assert expected in str(e), (reason, str(e))
        assert forbidden not in str(e), f"{reason} error wrongly mentions: {forbidden}"
# An unrecognised/future reason must still fail loudly, without misattributing it.
try:
    check_gemini_finish_reason("SOME_FUTURE_REASON", "m")
    raise AssertionError("expected raise for unknown reason")
except RuntimeError as e:
    assert "not one the harness has specific guidance for" in str(e)
    assert "training content" not in str(e)
print("OK Gemini finish_reason errors are cause-specific, unknown reasons fail generically")

# --------------------------------------------------------------- 15. prompt file ranking
# The real ansible-vars-001 failure: the description named `lib/ansible/vars/manager.py`
# explicitly, but an unrelated `config/manager.py` sharing only a basename ranked equal and,
# being large, exhausted the byte budget first. An exact path match must outrank a basename.
class _FakeFiles:
    def __init__(self, paths): self.paths = paths
    def list_files(self): return list(self.paths)
    def read_many(self, rel_paths, max_bytes=None): return {p: f"# {p}\n" for p in rel_paths}

solver = LLMSolver.__new__(LLMSolver)   # no provider/network needed to build a prompt
solver._files_inlined = 0
prompt = solver._build_prompt(
    _FakeFiles(["zz/unrelated.py", "lib/ansible/config/manager.py", "lib/ansible/vars/manager.py"]),
    "Bug in `combine_vars`. Location: `lib/ansible/vars/manager.py` handles this.",
)
order = [ln[4:] for ln in prompt.splitlines() if ln.startswith("### ")]
assert order[0] == "lib/ansible/vars/manager.py", order
assert order.index("lib/ansible/vars/manager.py") < order.index("lib/ansible/config/manager.py")
# Anti-memorization instruction must actually reach the model.
assert "Do NOT write code from memory" in prompt
# The SEARCH block IS the chain-of-extraction step: it can only match if the model copied the
# provided file verbatim rather than recalling it, which is what the <quote> step used to force.
assert "unified diff" in prompt.lower(), "the diff format must be instructed"
# SWE-bench Pro includes feature additions and refactors, not only bug fixes - its own
# issue_specificity taxonomy presumes that - so the prompt must not frame every task as a bug.
assert "## Task description" in prompt and "## Bug description" not in prompt
assert "fixing a bug" not in prompt.lower(), "task framing must not assume a bug fix"
print("OK prompt ranking: exact-path mention outranks a basename collision")

# The instructions must describe the format they actually expect, and must not frame every
# task as a bug fix - SWE-bench Pro includes feature additions and refactors, and its own
# issue_specificity taxonomy presumes non-bug types exist.
_ex = LLMSolver.__new__(LLMSolver); _ex._files_inlined = 0; _ex.append_prompt = None
_extext = _ex._build_prompt(_FakeFiles(["pkg/parser/reader.go"]), "add `read` validation")
_head = _extext[:_extext.index("## Task description")]
assert "unified diff" in _head.lower(), "instructions must ask for a unified diff"
assert "SEARCH/REPLACE" not in _head, "leftover search/replace wording"
assert "fixing a bug" not in _head.lower(), "task framing must not assume a bug fix"
assert "## Bug description" not in _extext
# The two format rules that cost real runs when the model got them wrong.
assert "leading space" in _head or "begin with a space" in _head, "hunk prefix rule missing"
print("OK prompt: asks for a unified diff, task framing not bug-specific")

# --------------------------------------------------------------- 16. run-time overrides
from agent_evals.solvers import get_solver, VALID_THINKING_LEVELS
from agent_evals.cli import _read_append_prompt

_ov = get_solver("llm", provider="gemini", temperature=0.7, thinking="high")
assert _ov.provider.TEMPERATURE == 0.7 and _ov.provider.THINKING_OVERRIDE == "high"
for _lvl in VALID_THINKING_LEVELS:                       # every advertised level is accepted
    get_solver("llm", provider="gemini", thinking=_lvl)
try:
    get_solver("llm", provider="gemini", thinking="ludicrous")
    raise AssertionError("expected an unknown thinking level to be rejected")
except ValueError as e:
    assert "expected one of" in str(e)

# --append-prompt must ADD to the prompt, never replace any of it. If a flag could drop the
# task description or the output contract, two runs of "the same" task would not be comparable
# and the verdict would look like a benchmark result while measuring something else.
_ap = get_solver("llm", provider="gemini", append_prompt="Prefer table-driven Go tests.")
_ap._files_inlined = 0
_aptext = _ap._build_prompt(_FakeFiles(["a.go"]), "THE-BUG-DESCRIPTION")
assert "THE-BUG-DESCRIPTION" in _aptext, "description must survive an append"
assert "unified diff" in _aptext.lower(), "output contract must survive an append"
assert "### a.go" in _aptext, "file contents must survive an append"
assert _aptext.rstrip().endswith("Prefer table-driven Go tests."), "appended text goes last"
# Absent flag changes nothing.
_noap = get_solver("llm", provider="gemini"); _noap._files_inlined = 0
assert "Additional guidance" not in _noap._build_prompt(_FakeFiles(["a.go"]), "d")

# @file form reads from disk; plain text is used verbatim.
_pf = tmp / "extra.txt"
_pf.write_text("from a file\n", encoding="utf-8")
assert _read_append_prompt(f"@{_pf}").strip() == "from a file"
assert _read_append_prompt("inline text") == "inline text"
assert _read_append_prompt(None) is None
print("OK run overrides: --temperature/--thinking validated, --append-prompt is additive only")

# --------------------------------------------------------------- 17. progress reporting
# A run can sit minutes inside one docker exec while `go test` compiles, which is
# indistinguishable from a hang when output is captured - a real run was reported as stuck
# while pegging 19 cores. Verbose must narrate; the default must stay silent for scripts.
from agent_evals.runner import StepReporter
_quiet_out, _loud_out = _io.StringIO(), _io.StringIO()
with _ctx.redirect_stdout(_quiet_out):
    _q = StepReporter(enabled=False); _q.step("building"); _q.note("hi"); _q.done()
with _ctx.redirect_stdout(_loud_out):
    _l = StepReporter(enabled=True); _l.step("building"); _l.note("hi"); _l.done()
assert _quiet_out.getvalue() == "", f"default must be silent: {_quiet_out.getvalue()!r}"
assert "building" in _loud_out.getvalue() and "done in" in _loud_out.getvalue()
assert "hi" in _loud_out.getvalue()
print("OK step reporting: narrates each phase with timings under --verbose, silent by default")

# The root cause of the ansible failure: the target file was silently truncated at 20KB while
# the code needing the fix lived at line 786, so the model reconstructed it from memory. A
# named target file must get a much larger allowance than a context file, and ANY truncation
# must be announced so the model knows the region is absent rather than guessing.
from agent_evals.solvers import (
    MAX_INLINED_FILES, MAX_SINGLE_FILE_BYTES, MAX_TOTAL_BYTES,
)
assert 20 <= MAX_INLINED_FILES <= 200, "count cap is a loose backstop; MAX_TOTAL_BYTES should bind"
# A realistic source file must fit comfortably: the whole point is that the model sees the
# file it has to edit in full. manager.py, the file this bug was found on, is ~35KB.
assert MAX_SINGLE_FILE_BYTES >= 200_000, "a normal source file must never be excluded for size"

# Files arrive WHOLE. A 35KB file (the real size of the file this bug was found on) must be
# passed through byte-for-byte, not trimmed. The reader must also be asked for more than the
# limit, otherwise "exactly N bytes" and "truncated at N" are indistinguishable.
realistic = "y" * 35_000
requested_caps = []
class _WholeFiles:
    def list_files(self): return ["pkg/target.py", "ctx/other.py"]
    def read_many(self, rel_paths, max_bytes=None):
        requested_caps.append(max_bytes)
        return {p: realistic for p in rel_paths}

s2 = LLMSolver.__new__(LLMSolver); s2._files_inlined = 0
p2 = s2._build_prompt(_WholeFiles(), "Fix it. Location: `pkg/target.py`")
assert requested_caps and requested_caps[0] > MAX_SINGLE_FILE_BYTES, (
    f"must request MORE than the limit to detect oversize files, got {requested_caps}"
)
body = p2.split("### pkg/target.py", 1)[1]
assert realistic in body, "the target file must appear in full, untrimmed"
assert "TRUNCATED" not in p2, "nothing should be truncated any more - whole files or nothing"

# An oversized blob is excluded ENTIRELY and named, never silently trimmed: an absent file is
# honest (the model can say it lacks it), a partial file is a trap that looks complete.
# Both paths are named in the description so both rank tier 0 - otherwise the relevance
# filter below would drop the second one for being irrelevant, and this check would pass for
# the wrong reason instead of exercising the size path it exists to cover.
class _HugeFile:
    def list_files(self): return ["pkg/target.py", "pkg/huge.py"]
    def read_many(self, rel_paths, max_bytes=None):
        return {"pkg/target.py": "ok", "pkg/huge.py": "z" * (MAX_SINGLE_FILE_BYTES + 10)}

s3 = LLMSolver.__new__(LLMSolver); s3._files_inlined = 0
p3 = s3._build_prompt(_HugeFile(), "Fix it. Location: `pkg/target.py` and `pkg/huge.py`")
assert "## Files NOT shown" in p3 and "pkg/huge.py" in p3.split("## Files NOT shown")[1]
assert "### pkg/huge.py" not in p3, "oversized file must be excluded, not partially shown"
print("OK prompt budget: whole files only, oversized ones excluded and declared")

# --------------------------------------------------------------- 18. relevance filtering
# Tier 3 means "nothing about this file matches the task". Including it is not free padding:
# ties break alphabetically, so on a real run CHANGELOG.md (52KB) and LICENSE (35KB) won the
# budget while the file that had to change was 9.6% of the prompt. Dropping tier 3 is only
# safe while a stronger signal exists, which is exactly the condition below.
class _MixedRepo:
    FILES = ["CHANGELOG.md", "LICENSE", "pkg/target.py", "pkg/unrelated.py",
             "vendor/lib/bundled.py", "docs/guide.py"]
    def list_files(self): return list(self.FILES)
    def read_many(self, rel_paths, max_bytes=None): return {p: "content" for p in rel_paths}

def _inlined(description, symbols=()):
    class _R(_MixedRepo):
        def grep_files(self, syms, max_results=100): return list(symbols)
    s = LLMSolver.__new__(LLMSolver); s._files_inlined = 0; s.append_prompt = None
    p = s._build_prompt(_R(), description)
    return [b.split("\n")[0].strip() for b in p.split("\n### ")[1:]]

# Tier 0 present -> the irrelevant tail is dropped, prose and vendored trees with it.
strong = _inlined("Fix it. Location: `pkg/target.py`")
assert "pkg/target.py" in strong, strong
assert "CHANGELOG.md" not in strong and "LICENSE" not in strong, f"prose survived: {strong}"
assert "vendor/lib/bundled.py" not in strong, f"vendored tree survived: {strong}"
assert "pkg/unrelated.py" not in strong, f"tier-3 file survived a strong signal: {strong}"

# Tier 1 counts as a strong signal too - a grep hit is evidence even with no path named. The
# identifier must be backticked, because that is what symbols_in_description extracts; without
# backticks no symbol is found, grep is never called, and this would silently test nothing.
by_symbol = _inlined("Fix the `parseThing` helper.", symbols=["pkg/target.py"])
assert "pkg/target.py" in by_symbol and "pkg/unrelated.py" not in by_symbol, by_symbol

# No tier 0 and no tier 1 -> tier 3 comes back, because then it is all there is. Prose still
# sorts last so it loses the budget rather than winning it alphabetically.
weak = _inlined("Something is broken somewhere.")
assert "pkg/unrelated.py" in weak, f"fallback must keep tier 3: {weak}"
assert weak.index("pkg/unrelated.py") < weak.index("CHANGELOG.md"), (
    f"prose must rank below real source even in fallback: {weak}"
)
print("OK relevance filter: tier 3 dropped when a stronger signal exists, restored when not")

# --------------------------------------------------------------- 19. the closing block
# Google's Gemini 3 guidance for long prompts: put the core request and the most critical
# restrictions LAST, negative constraints at the end - and keep them terse, because the model
# "may over-analyze verbose or overly complex prompt engineering techniques".
_c = LLMSolver.__new__(LLMSolver); _c._files_inlined = 0; _c.append_prompt = None
_cp = _c._build_prompt(_MixedRepo(), "Fix it. Location: `pkg/target.py`")
_tail = _cp[_cp.index("## Before you answer"):]
assert _cp.index("## Before you answer") > _cp.index("## File contents"), (
    "the checklist must come AFTER the file contents - instructions at the top of a 130KB "
    "prompt sit tens of thousands of tokens before the model starts generating"
)
assert "regression" in _tail.lower() and "pass" in _tail.lower(), (
    "the closing block must state that existing behaviour is covered by tests that pass, so "
    "breaking one is a regression - this is what protects the pass2pass bucket"
)
assert _tail.rstrip().endswith("Change the fewest lines that satisfy it."), (
    f"minimality must be the FINAL line: {_tail[-120:]!r}"
)
# A prompt regression guard. This exact instruction, adapted from SWE-agent's agentic
# template, told the model to handle "awkward inputs" and "boundaries" beyond the described
# ones. An agent can afford that because it runs the tests; in one shot the model invented
# robustness, rewrote a working parser, and broke three pass2pass tests. It must not return.
for _banned in ("awkward input", "boundaries of any range", "usually the easy case"):
    assert _banned not in _cp, (
        f"{_banned!r} is back in the prompt - it invites speculative hardening beyond the "
        f"task, which is what turned harmless UNSOLVED runs into REGRESSIONs"
    )
print("OK closing block: after the files, minimality last, no speculative-hardening rule")

# --------------------------------------------------------------- 20. provider output caps
# A real MAX_TOKENS failure: Gemini 2.5 draws thinking tokens from the same allowance as the
# visible answer, so an 8192 cap was consumed by reasoning before any diff was emitted. The
# cap is per-provider because the true ceiling is model-specific - gpt-4o rejects >16384 -
# so it must never be "fixed" by raising one shared global value.
from agent_evals.solvers import PROVIDERS, GeminiProvider, OpenAIProvider
for _name, _cls in PROVIDERS.items():
    cap = _cls.MAX_OUTPUT_TOKENS
    assert isinstance(cap, int) and cap >= 8192, f"{_name}: implausible output cap {cap}"
assert OpenAIProvider.MAX_OUTPUT_TOKENS <= 16_384, "gpt-4o rejects a cap above 16384"
# Thinking must be explicitly bounded, not left at the API default that caused the failure.
# Thinking must be bounded, not AUTOMATIC(-1): unbounded reasoning can consume the whole
# allowance and silently starve the answer. It must also not be 0 - the self-inconsistencies
# seen in practice (renaming a variable then using the old name) are what reasoning catches.
assert 0 < GeminiProvider.THINKING_BUDGET < GeminiProvider.MAX_OUTPUT_TOKENS, (
    "thinking budget must be enabled and strictly smaller than the output cap"
)
assert GeminiProvider.MAX_OUTPUT_TOKENS >= 32_768, "too tight for a real multi-file diff"
print("OK provider output caps: per-provider, model-appropriate, thinking bounded")

# --------------------------------------------------------------- 21. symbol-based ranking
# Most bug descriptions name functions and classes but NO file path. vuls-redhat-001 names
# `parseInstalledPackagesLine` and `splitFileName` and never mentions scanner/redhatbase.go, so
# path- and basename-ranking had nothing to work with: the model got twelve unrelated files and
# correctly reported it could not see the code. Grepping the repo for the named symbols is the
# signal that finds it.
from agent_evals.solvers import symbols_in_description

assert symbols_in_description("calls `parseInstalledPackagesLine` and `splitFileName`") == [
    "parseInstalledPackagesLine", "splitFileName"
]
# Noise and prose in backticks must not become grep patterns.
assert symbols_in_description("`a` `os` `id` raises `TypeError` on `None` for `str`") == ["TypeError"]
assert symbols_in_description("no backticks here at all") == []
# Deterministic: order preserved, duplicates collapsed - the grep must be reproducible.
assert symbols_in_description("`beta` then `alpha` then `beta`") == ["beta", "alpha"]

# A file containing a named symbol must outrank a mere basename collision, and must be inlined
# even when the description names no path at all.
class _GrepFiles:
    def list_files(self):
        # aaa/scanner.py sorts FIRST alphabetically and its stem really does appear in the
        # description, so it is a genuine tier-2 collision. (An earlier version used
        # "decoy_scanner.py", whose stem never matched - it was tier 3, so the ordering
        # assertion below held trivially and proved nothing.)
        return ["aaa/scanner.py", "scanner/redhatbase.go", "zzz/unrelated.py"]
    def read_many(self, rel_paths, max_bytes=None):
        return {p: f"# {p}\n" for p in rel_paths}
    def grep_files(self, symbols, max_results=100):
        assert "parseInstalledPackagesLine" in symbols, symbols
        return ["scanner/redhatbase.go"]

_g = LLMSolver.__new__(LLMSolver); _g._files_inlined = 0
_gp = _g._build_prompt(_GrepFiles(), "Bug in `parseInstalledPackagesLine`. See scanner notes.")
_order = [ln[4:] for ln in _gp.splitlines() if ln.startswith("### ")]
assert _order[0] == "scanner/redhatbase.go", _order
# The content match (tier 1) must beat the basename collision (tier 2) despite sorting later.
assert _order.index("scanner/redhatbase.go") < _order.index("aaa/scanner.py"), _order
# And the file matching nothing is dropped entirely now that a stronger signal exists.
assert "zzz/unrelated.py" not in _order, _order

# Ranking must DEGRADE, never fail, if grep is unavailable or errors.
class _NoGrep:
    def list_files(self): return ["x.py"]
    def read_many(self, rel_paths, max_bytes=None): return {p: "x\n" for p in rel_paths}

class _BrokenGrep(_NoGrep):
    def grep_files(self, symbols, max_results=100): raise RuntimeError("grep exploded")

for _reader in (_NoGrep(), _BrokenGrep()):
    _d = LLMSolver.__new__(LLMSolver); _d._files_inlined = 0
    assert "### x.py" in _d._build_prompt(_reader, "Bug in `someSymbol`"), type(_reader).__name__
print("OK symbol ranking: content match outranks basename, degrades safely without grep")

# --------------------------------------------------------------- 22. byte-exact file reads
# read_many's separator is printed as "\n@@marker@@<path>@@\n", so the newline opening the NEXT
# separator used to land at the end of THIS file's body - every file but the last came back one
# byte too long. Harmless while the bytes were only shown in a prompt; now they are the ground
# truth the unified diff is generated from, so a phantom trailing newline put a blank line in
# the diff's final context and git rejected the patch. Parsing is checked directly here.
from agent_evals.runner import ContainerFiles

_files = {"a.py": "first\ncontent\n", "b.py": "middle\n", "c.py": "last, no trailing sep\n"}

class _FakeDocker:
    """Replays what the container would emit, using the marker read_many actually generated."""
    def exec(self, container, cmd, **kw):
        marker = re.search(r"@@([0-9a-f]{32})@@", cmd[-1]).group(1)
        return 0, "".join(f"\n@@{marker}@@{p}@@\n{b}" for p, b in _files.items()), ""

_cf = ContainerFiles.__new__(ContainerFiles)
_cf.docker, _cf.container, _cf.workdir = _FakeDocker(), "c", "/workspace"
_parsed = _cf.read_many(list(_files))
for _p, _expected in _files.items():
    assert _parsed[_p] == _expected, (
        f"{_p}: expected {_expected!r} got {_parsed[_p]!r} - a spurious trailing newline here "
        f"corrupts every generated diff"
    )
print("OK read_many: byte-exact for first/middle/last file, no separator bleed")

print("\nALL UNIT CHECKS PASSED")
