#!/bin/sh
# Cross-platform verification suite, executed INSIDE a Linux container.
#
# Everything here runs as a non-root Linux user driving the real Docker daemon, so a pass
# means the CLI works on Linux (and by extension macOS, which shares POSIX semantics and
# the same Docker Desktop VM boundary).
set -u

PASS=0
FAIL=0

check() {  # check <description> <expected-exit> <command...>
    desc="$1"; expected="$2"; shift 2
    "$@" >/tmp/out.txt 2>&1
    actual=$?
    if [ "$actual" -eq "$expected" ]; then
        echo "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $desc (expected exit $expected, got $actual)"
        tail -20 /tmp/out.txt | sed 's/^/        /'
        FAIL=$((FAIL + 1))
    fi
}

expect_in_output() {  # expect_in_output <description> <needle> <command...>
    desc="$1"; needle="$2"; shift 2
    "$@" >/tmp/out.txt 2>&1
    if grep -q "$needle" /tmp/out.txt; then
        echo "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $desc (no '$needle' in output)"
        tail -20 /tmp/out.txt | sed 's/^/        /'
        FAIL=$((FAIL + 1))
    fi
}

echo "=============================================================="
echo " Platform: $(uname -s) $(uname -m)   Python: $(python --version 2>&1)"
echo " Running as uid=$(id -u) gid=$(id -g)"
echo "=============================================================="

echo
echo "[1] Docker reachable from inside the container"
check "docker daemon responds" 0 docker version

echo
echo "[2] Unit-level checks (no Docker)"
check "bundle parses on Linux" 0 python -c "
from pathlib import Path
from agent_evals.bundle import TaskBundle
b = TaskBundle(Path('bundles/toy-calc-001'))
assert b.task.hidden_paths == ('tests/test_add.py',), b.task.hidden_paths
assert b.task.protected_paths
"
check "ledger migration + allowlist" 0 python -c "
import tempfile, sqlite3, json
from pathlib import Path
from agent_evals.db import RunDB
p = Path(tempfile.mkdtemp())/'legacy.db'
c = sqlite3.connect(p)
c.execute('''CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  command TEXT NOT NULL, task_id TEXT, args_json TEXT NOT NULL, status TEXT NOT NULL,
  results_json TEXT, log TEXT)''')
c.commit(); c.close()
with RunDB(p) as db:
    rid = db.create_run('run','b',{'bundle':'b'})
    db.update_run(rid, captured_diff='d')
    assert db.get_run(rid)['captured_diff'] == 'd'
    try:
        db.update_run(rid, **{'bogus; DROP TABLE runs--':'x'}); raise SystemExit('allowlist failed')
    except ValueError: pass
"
check "diff normalization" 0 python -c "
from agent_evals.runner import normalize_captured_diff
raw='diff --git a/baseline/x.py b/current/x.py\n--- a/baseline/x.py\n+++ b/current/x.py\n'
n=normalize_captured_diff(raw)
assert 'baseline' not in n and 'current' not in n, n
"

echo
echo "[3] Core flow against the real daemon (toy-calc-001)"
check "evals init (image cached)" 0 evals init bundles/toy-calc-001
check "evals validate -> baseline invariant holds" 0 evals validate bundles/toy-calc-001
check "evals run --solver stub -> UNSOLVED (exit 1)" 1 evals run bundles/toy-calc-001 --solver stub
check "evals run --solver oracle -> SOLVED (exit 0)" 0 evals run bundles/toy-calc-001 --solver oracle --keep-artifacts

echo
echo "[4] No repo files extracted to host filesystem"
if ls /tmp/evals-solve-* >/dev/null 2>&1; then
    echo "  FAIL  repo extraction dirs found in /tmp"
    FAIL=$((FAIL + 1))
else
    echo "  PASS  no evals-solve-* dirs in /tmp"
    PASS=$((PASS + 1))
fi

echo
echo "[5] Ledger-driven commands"
LAST_ORACLE=$(python -c "
from agent_evals.db import RunDB
with RunDB() as db:
    r=[x for x in db.list_runs(50) if x['command']=='run' and x.get('captured_diff')]
    print(r[0]['run_id'] if r else '')
")
if [ -n "$LAST_ORACLE" ]; then
    check "evals replay <id> -> SOLVED, no solver" 0 evals replay "$LAST_ORACLE"
else
    echo "  FAIL  no run with a stored diff to replay"
    FAIL=$((FAIL + 1))
fi
check "evals grade --diff-file (external patch)" 0 \
    evals grade bundles/toy-calc-001 --diff-file bundles/toy-calc-001/patch.diff

echo
echo "[6] Anti-tamper: a cheating conftest.py must NOT yield SOLVED"
cat > /tmp/cheat.diff <<'CHEAT'
diff --git a/conftest.py b/conftest.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/conftest.py
@@ -0,0 +1,4 @@
+def pytest_collection_modifyitems(config, items):
+    for item in items:
+        if hasattr(item, "obj"):
+            item.obj = lambda *a, **k: None
CHEAT
expect_in_output "cheating patch flagged as tampering" "modified test infrastructure" \
    evals grade bundles/toy-calc-001 --diff-file /tmp/cheat.diff
check "cheating patch does NOT score SOLVED (exit 1)" 1 \
    evals grade bundles/toy-calc-001 --diff-file /tmp/cheat.diff

echo
echo "[7] Error UX"
check "missing bundle -> clean exit 1" 1 evals init bundles/does-not-exist
check "unknown provider -> clean exit 1" 1 evals run bundles/toy-calc-001 --solver llm --provider bogus

echo
echo "[8] Cleanup"
check "no leftover containers" 0 sh -c '[ -z "$(docker ps -aq --filter name=evals-)" ]'

echo
echo "=============================================================="
echo " RESULT: $PASS passed, $FAIL failed"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
