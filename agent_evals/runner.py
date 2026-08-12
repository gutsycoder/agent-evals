"""Orchestrates evals init / validate / solve / grade flows across containers.

The solve and grade phases are deliberately separate methods. `run()` is just their
composition, which is what makes `evals resume`, `evals replay`, and `evals grade
--diff-file` possible: grading an existing patch never has to involve a solver.
"""

from __future__ import annotations

import io
import logging
import re
import shlex
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from agent_evals.bundle import TaskBundle, TaskSpec
from agent_evals.docker_mgr import DockerManager
from agent_evals.solvers import get_solver

logger = logging.getLogger(__name__)

IMAGE_NAMESPACE = "evals"
NON_ROOT_USER = "appuser"
# Fallback workdir, used when an image declares none of its own. A prebuilt benchmark image
# usually does declare one (SWE-bench Pro's are /app) and ships the repo there with its
# dependencies installed against that path - so we work THERE rather than cloning a second
# copy here. Patching a parallel checkout the environment never imports is how a correct patch
# came to be graded as a failure; see Runner._resolve_workdir.
DEFAULT_REPO_WORKDIR = "/workspace"
BASELINE_DIR = "/baseline"
CURRENT_SNAPSHOT_DIR = "/current"
_BASELINE_NAME = BASELINE_DIR.strip("/")
_CURRENT_NAME = CURRENT_SNAPSHOT_DIR.strip("/")

BUCKET_PASS2PASS = "pass2pass"
BUCKET_FAIL2PASS = "fail2pass"
ALL_BUCKETS = (BUCKET_PASS2PASS, BUCKET_FAIL2PASS)

# Caps on container-produced data crossing to the host (see DESIGN_NOTES.md §6).
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_STORED_LOG_CHARS = 200_000
MAX_READ_FILES = 300
MAX_FILE_BYTES = 20_000

DOCKERFILE_TEMPLATE = """FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive \\
    PIP_NO_CACHE_DIR=1

# Root-level image prep from task.json's setup_cmd (installs git + the test runner).
RUN {setup_cmd}

# git is used by the harness itself (clone, apply patches, capture diffs), so fail the
# build here with a clear message rather than mysteriously later.
RUN command -v git >/dev/null 2>&1 || ( \\
      echo "ERROR: 'git' is not on PATH after setup_cmd. The harness requires git inside" \\
      && echo "the image. Add it to setup_cmd in task.json (e.g. 'apk add --no-cache git'" \\
      && echo "for Alpine, 'apt-get install -y git' for Debian/Ubuntu)." \\
      && exit 1 )

WORKDIR {workdir}

# Fetch only the pinned commit rather than `git clone` + `git checkout`. A full clone of
# a large repo (teleport, ansible) is gigabytes of history we immediately stop caring
# about, and it lands in every image and every /baseline copy. GitHub serves arbitrary
# reachable SHAs, but not every host does, so fall back to a full clone when it refuses.
# Either way HEAD ends up detached at exactly {commit}, which is the contract the rest of
# the harness relies on.
# Idempotent: {workdir} may already contain the repo. Prebuilt benchmark images ship it at
# their own WORKDIR with dependencies installed and pointing there, and in that case this is
# the directory we work in - so `git init` has to tolerate an existing repo, and the commit is
# often already present locally (no network needed). Falls back to fetching, then to a full
# clone, for a plain base image where {workdir} starts empty.
RUN git init -q \\
    && (git remote add origin {repo} 2>/dev/null || git remote set-url origin {repo}) \\
    && if git rev-parse --verify -q {commit}^{{commit}} >/dev/null; then \\
           git checkout -q {commit}; \\
       elif git fetch -q --depth 1 origin {commit} 2>/dev/null; then \\
           git checkout -q FETCH_HEAD; \\
       else \\
           echo "note: host refused a shallow fetch of {commit}; falling back to a full clone" >&2; \\
           git fetch -q origin; \\
           git checkout -q {commit}; \\
       fi \\
    && test "$(git rev-parse HEAD)" = "{commit}"

# Hide exactly the declared pass2pass/fail2pass paths that already exist in the base
# repo (task.json's hidden_paths) so the solver can't see them - everything else in
# the repo, including any other tests, stays visible.
{strip_step}

RUN {deps_cmd}

# Freeze a snapshot of the tree *after* deps_cmd, so it matches exactly what the
# solver starts from (e.g. editable-install .egg-info metadata is already present on
# both sides). The solver's changes are captured later by diffing a fresh copy of
# {workdir} against this frozen {baseline_dir} (git diff --no-index) rather than
# against git history - HEAD stays pinned at {commit}, no synthetic commits needed.
RUN cp -a {workdir} {baseline_dir} \\
    && rm -rf {baseline_dir}/.git \\
    && mkdir -p {current_dir}

# Non-root user for running solver-produced code and tests. useradd is Debian-ish,
# adduser is BusyBox/Alpine - try both so this works across base images.
RUN ( useradd --create-home --shell /bin/sh {user} 2>/dev/null \\
      || adduser -D -s /bin/sh {user} 2>/dev/null \\
      || echo "warning: could not create {user}; container will run as its default user" ) \\
    && chown -R {user} {workdir} {baseline_dir} {current_dir} 2>/dev/null || true

USER {user}

# Overridden at runtime with `sleep infinity`; /bin/sh exists on every base image.
CMD ["/bin/sh"]
"""


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------

@dataclass
class SolveResult:
    """Output of the solve phase (container A). `diff` is the durable artifact."""
    diff: str = ""
    patch_applied: bool = False
    patch_error: Optional[str] = None
    solver_metadata: Optional[dict[str, Any]] = None
    prompt: Optional[str] = None
    raw_response: Optional[str] = None
    raw_solver_diff: Optional[str] = None


class SolveFailure(RuntimeError):
    """Solve failed, but carries the partial result so artifacts can still be written.

    The prompt and raw response are the only useful evidence when a solve fails, and losing
    them means the next debugging step is "run it again and hope". The original error is kept
    as the message so callers and the ledger still report the real cause.
    """

    def __init__(self, partial: "SolveResult", cause: BaseException) -> None:
        # The original exception's message, verbatim. An earlier version looked up a "failed"
        # key that nothing ever set, so every solver failure - a rejected API key, a 429, an
        # exhausted output budget - surfaced as the single word "solver failed", replacing a
        # message that said exactly what to do with one that said nothing.
        super().__init__(str(cause) or cause.__class__.__name__)
        self.partial = partial


@dataclass
class GradeResult:
    """Output of the grade phase (container B)."""
    verdict: str = "UNSOLVED"
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    table: str = ""
    touched_protected_paths: list[str] = field(default_factory=list)
    reapply_error: Optional[str] = None


# --------------------------------------------------------------------------------------
# Module-level helpers
# --------------------------------------------------------------------------------------

def image_tag_for(task_id: str, commit: str) -> str:
    return f"{IMAGE_NAMESPACE}/{task_id}:{commit}"


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 with newline='' so captured diffs keep exact \\n bytes on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _render_strip_step(hidden_paths: tuple[str, ...]) -> str:
    if not hidden_paths:
        return "# no hidden_paths declared in task.json - nothing stripped from the image"
    quoted = " ".join(shlex.quote(p) for p in hidden_paths)
    return f"RUN rm -rf {quoted}"


def render_dockerfile(task: TaskSpec, workdir: str = DEFAULT_REPO_WORKDIR) -> str:
    return DOCKERFILE_TEMPLATE.format(
        base_image=task.base_image,
        setup_cmd=task.setup_cmd,
        workdir=workdir,
        repo=task.repo,
        commit=task.commit,
        deps_cmd=task.deps_cmd,
        user=NON_ROOT_USER,
        strip_step=_render_strip_step(task.hidden_paths),
        baseline_dir=BASELINE_DIR,
        current_dir=CURRENT_SNAPSHOT_DIR,
    )


def normalize_captured_diff(diff_text: str) -> str:
    """Rewrite `git diff --no-index /baseline /current` headers into ordinary a/ b/ form.

    The raw capture yields `a/baseline/pkg/mod.py` / `b/current/pkg/mod.py`, which needs
    `git apply -p2` and reads oddly as a stored artifact. Normalizing to `a/pkg/mod.py` /
    `b/pkg/mod.py` means every diff we handle applies with plain `-p1` and the stored patch
    is a normal one that a human can `git apply` by hand.

    Only header lines are touched, so file *content* containing similar text is safe.
    (Edge case: a repo with a real top-level directory literally named `baseline` or
    `current` would be ambiguous here - vanishingly unlikely, and it would still apply,
    just at the wrong prefix depth.)
    """
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            line = line.replace(f" a/{_BASELINE_NAME}/", " a/").replace(
                f" b/{_CURRENT_NAME}/", " b/"
            )
        elif line.startswith("--- "):
            line = line.replace(f"--- a/{_BASELINE_NAME}/", "--- a/", 1)
        elif line.startswith("+++ "):
            line = line.replace(f"+++ b/{_CURRENT_NAME}/", "+++ b/", 1)
        out.append(line)
    return "".join(out)


_DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.MULTILINE)


def diff_touched_paths(diff_text: str) -> list[str]:
    """Repo-relative paths a (normalized) unified diff modifies."""
    return sorted({m.group(1) for m in _DIFF_PATH_RE.finditer(diff_text) if m.group(1) != "ev/null"})


def parse_junit_xml(path: Path) -> list[dict[str, Any]]:
    """Parse a JUnit XML report into a flat list of testcase outcomes."""
    raw = path.read_bytes()
    if len(raw) > MAX_XML_BYTES:
        raise RuntimeError(
            f"JUnit XML at {path} is {len(raw)} bytes, above the {MAX_XML_BYTES}-byte cap. "
            f"Refusing to parse container-produced data this large."
        )
    # Cheap guard against entity-expansion ("billion laughs") attacks: legitimate JUnit XML
    # never declares entities, and ElementTree will happily expand them. See DESIGN_NOTES.md §6.
    if b"<!ENTITY" in raw:
        raise RuntimeError(
            f"JUnit XML at {path} declares XML entities, which JUnit output never needs. "
            f"Refusing to parse (possible entity-expansion attack)."
        )

    root = ET.fromstring(raw)
    cases: list[dict[str, Any]] = []
    for testcase in root.iter("testcase"):
        outcome = "passed"
        message: Optional[str] = None
        for tag in ("failure", "error", "skipped"):
            node = testcase.find(tag)
            if node is not None:
                outcome = "failed" if tag == "failure" else tag
                message = node.get("message")
                break
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        cases.append({
            "id": f"{classname}::{name}" if classname else name,
            "name": name,
            "classname": classname,
            "outcome": outcome,
            "message": message,
        })
    return cases


def manifest_test_key(test_id: str) -> str:
    """Reduce a benchmark test id to the `name` its JUnit reporter will emit.

    Bucket attribution compares manifest entries against `<testcase name=...>`, so the
    two have to be expressed the same way. Each runner writes ids differently:

      pytest  `test/units/utils/test_vars.py::TestVars::test_x`  -> `test_x`
              (pytest puts the module+class in `classname` and only the function in `name`)
      jest    `test/database.js | Test database | should work`   -> `Test database should work`
              (jest-junit's default titleTemplate is `{classname} {title}`, i.e. the
              ancestor describe titles space-joined, then the test title; the leading
              segment is the file, which jest does not repeat in `name`)
      go      `TestHTTPConnStateReporter/without_client_certs`   -> unchanged
              (go-junit-report emits the test function name verbatim, `/` being the
              *subtest* separator - not a path, which is why it must not be split)

    Anything unrecognised is passed through, which is the right default: a bundle author
    writing the manifest by hand naturally writes the name the reporter emits.
    """
    test_id = test_id.strip()
    if "::" in test_id:
        return test_id.rsplit("::", 1)[-1]
    if "|" in test_id:
        return " ".join(p.strip() for p in test_id.split("|")[1:] if p.strip())
    return test_id


def expected_test_names(bucket_dir: Path) -> set[str]:
    """Test names a bucket declares in _selected_tests.txt (scaffold-generated).

    Returns an empty set for hand-authored bundles without the file, in which case
    missing-test detection is skipped.
    """
    manifest = bucket_dir / "_selected_tests.txt"
    if not manifest.is_file():
        return set()
    return {manifest_test_key(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()}


def _partition_cases(
    cases: list[dict[str, Any]], bundle: TaskBundle, exit_code: int
) -> dict[str, dict[str, Any]]:
    """Split one test run's results into the two buckets, by declared test name.

    The bucket manifests (`_selected_tests.txt`) are the source of truth for which test
    belongs where - the same file routinely contains both pass2pass and fail2pass tests.
    Tests present in the run but claimed by neither bucket are ignored: a file may hold
    unrelated tests the benchmark never certified.
    """
    bucket_dirs = {
        BUCKET_PASS2PASS: bundle.pass2pass_dir,
        BUCKET_FAIL2PASS: bundle.fail2pass_dir,
    }
    manifests = {b: expected_test_names(d) for b, d in bucket_dirs.items()}
    if not any(manifests.values()):
        raise RuntimeError(
            f"bundle {bundle.path} has no _selected_tests.txt in either bucket, so results "
            f"cannot be attributed to pass2pass vs fail2pass. Add one per bucket listing the "
            f"test names that bucket owns (one per line)."
        )

    observed = {c["name"] for c in cases}
    out: dict[str, dict[str, Any]] = {}
    for bucket, wanted in manifests.items():
        out[bucket] = {
            "exit_code": exit_code,
            "tests": [c for c in cases if c["name"] in wanted],
            # A test that vanished - deleted by the solver, or a collection error - is
            # simply absent from the XML, so `all(passed)` over the survivors would still
            # be True. Surface it instead.
            "missing_tests": sorted(wanted - observed),
        }
    return out


def _bucket_passes_expectation(bucket: str, cases: list[dict[str, Any]], missing: list[str]) -> bool:
    if not cases or missing:
        return False
    if bucket == BUCKET_PASS2PASS:
        return all(c["outcome"] == "passed" for c in cases)
    return all(c["outcome"] != "passed" for c in cases)


def render_bucket_table(buckets: dict[str, dict[str, Any]], expected_label: dict[str, str]) -> str:
    """expected_label maps bucket -> 'passed' or 'not passed', for display and per-row OK/FAIL."""
    headers = ("Bucket", "Test", "Outcome", "Expected", "OK")
    rows: list[tuple[str, str, str, str, str]] = []
    for bucket, data in buckets.items():
        expected = expected_label[bucket]
        for case in data["tests"]:
            ok = (case["outcome"] == "passed") if expected == "passed" else (case["outcome"] != "passed")
            rows.append((bucket, case["id"], case["outcome"], expected, "OK" if ok else "FAIL"))
        for name in data.get("missing_tests", []):
            rows.append((bucket, name, "MISSING", expected, "FAIL"))

    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]

    def fmt_row(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths))

    lines = [fmt_row(headers), fmt_row(tuple("-" * w for w in widths))]
    lines.extend(fmt_row(row) for row in rows)

    lines.append("")
    for bucket, data in buckets.items():
        status = "OK" if data["ok"] else "FAIL"
        note = ""
        if data.get("missing_tests"):
            note = f", {len(data['missing_tests'])} MISSING"
        lines.append(
            f"{bucket}: {status} ({len(data['tests'])} test(s){note}, "
            f"expected {expected_label[bucket]})"
        )

    return "\n".join(lines)


class ContainerFiles:
    """Reads repo files out of a container without ever landing them on host disk.

    Replaces an earlier design that tarred the whole workspace to a host temp dir. Only
    the bytes the prompt actually inlines cross the boundary, which is also what
    SWE-agent/OpenHands do (execute a read command, ship back the output).
    """

    def __init__(self, docker: DockerManager, container: str, workdir: str = DEFAULT_REPO_WORKDIR) -> None:
        self.docker = docker
        self.container = container
        self.workdir = workdir
        self._cached_list: Optional[list[str]] = None

    def list_files(self) -> list[str]:
        if self._cached_list is not None:
            return self._cached_list
        exit_code, stdout, stderr = self.docker.exec(
            self.container,
            ["sh", "-c", f"cd {self.workdir} && find . -type f -not -path './.git/*' | sed 's|^\\./||'"],
        )
        if exit_code != 0:
            raise RuntimeError(f"failed to list files in container: {stderr}")
        self._cached_list = sorted(line.strip() for line in stdout.splitlines() if line.strip())
        return self._cached_list

    def grep_files(self, symbols: list[str], max_results: int = 100) -> list[str]:
        """Repo-relative paths of files containing any of `symbols`, searched IN the container.

        This is the signal that fixes the common case where a bug description names functions
        and classes but no file path - which is most of them. `vuls-redhat-001` names
        `parseInstalledPackagesLine` and `splitFileName` and never mentions
        `scanner/redhatbase.go`, so path- and filename-based ranking had no way to find it and
        the model was handed twelve unrelated files.

        Runs as a single `grep -l` inside the container and returns only PATHS, so the cost is
        one exec and a few hundred bytes crossing the boundary - not file content.
        """
        symbols = [s for s in symbols if s][:20]
        if not symbols:
            return []
        # -F: fixed strings, so regex metacharacters in an identifier can't misbehave.
        # -l: names only. -I: skip binaries. Errors suppressed so an unreadable path is skipped.
        patterns = " ".join(f"-e {shlex.quote(s)}" for s in symbols)
        script = (
            f"cd {self.workdir} && grep -rlFI {patterns} . "
            f"--exclude-dir=.git 2>/dev/null | head -n {max_results} || true"
        )
        exit_code, stdout, _ = self.docker.exec(
            self.container, ["sh", "-c", script], warn_on_failure=False
        )
        if exit_code != 0 and not stdout.strip():
            return []
        out: list[str] = []
        for line in stdout.splitlines():
            path = line.strip()
            if path.startswith("./"):
                path = path[2:]
            if path:
                out.append(path)
        return out

    def read_many(self, rel_paths: list[str], max_bytes: Optional[int] = None) -> dict[str, str]:
        """Batch-read files in ONE docker exec, capping each at `max_bytes`.

        One exec per file would mean hundreds of subprocess round-trips; batching keeps it
        to a single call. `head -c` enforces the cap at the source, so a giant file never
        crosses the boundary in the first place.

        `max_bytes` is a parameter rather than a fixed constant because the caller is the only
        one who knows how much it needs. This was a real bug: the cap used to be hardcoded to
        MAX_FILE_BYTES here, so a caller that had raised its own limit still silently received
        a truncated file - the file needing a fix arrived cut off at 20,000 bytes, the model
        could not see the code it had to change, and it invented a replacement. Truncating at
        the source is invisible to the caller, so the decision belongs to the caller.
        """
        rel_paths = rel_paths[:MAX_READ_FILES]
        if not rel_paths:
            return {}
        cap = MAX_FILE_BYTES if max_bytes is None else max_bytes
        marker = uuid.uuid4().hex
        parts = " ".join(shlex.quote(p) for p in rel_paths)
        script = (
            f"cd {self.workdir} && for f in {parts}; do "
            f'printf "\\n@@{marker}@@%s@@\\n" "$f"; '
            f'head -c {cap} "$f" 2>/dev/null || true; '
            f"done"
        )
        exit_code, stdout, stderr = self.docker.exec(self.container, ["sh", "-c", script])
        if exit_code != 0:
            raise RuntimeError(f"failed to read files in container: {stderr}")

        contents: dict[str, str] = {}
        chunks = stdout.split(f"@@{marker}@@")[1:]
        for index, chunk in enumerate(chunks):
            head, sep, body = chunk.partition("@@\n")
            if not sep:
                continue
            # Each separator is printed as "\n@@marker@@<path>@@\n", so the newline that opens
            # the NEXT separator lands at the end of THIS file's body. Every file except the
            # last therefore arrives one byte too long. That was invisible while these bytes
            # were only shown in a prompt, but they are now the ground truth the unified diff
            # is generated from - a phantom trailing newline put a blank line in the diff's
            # final context that git could not match, and the patch was rejected.
            if index < len(chunks) - 1 and body.endswith("\n"):
                body = body[:-1]
            contents[head.strip()] = body
        return contents


class StepReporter:
    """Prints each phase with elapsed time, so a long run isn't a blank screen.

    The harness spends minutes inside single `docker exec` calls - `go test` compiles the
    package before running anything - and with output captured there is nothing to watch. That
    is indistinguishable from a hang: a real run was reported as "stuck" while it was sitting
    at 1900% CPU compiling. Silence needs a label attached to it.

    Off by default so scripted output stays clean; `--verbose` turns it on.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self._started: Optional[float] = None
        self._label = ""

    def step(self, label: str) -> None:
        self.done()
        self._label, self._started = label, time.monotonic()
        if self.enabled:
            print(f"  -> {label} ...", flush=True)

    def done(self) -> None:
        if self.enabled and self._started is not None:
            print(f"     {self._label} done in {time.monotonic() - self._started:.1f}s", flush=True)
        self._started = None

    def note(self, message: str) -> None:
        if self.enabled:
            print(f"     {message}", flush=True)


class Runner:
    def __init__(
        self, docker: Optional[DockerManager] = None, reporter: Optional[StepReporter] = None
    ) -> None:
        self.docker = docker or DockerManager()
        self.report = reporter or StepReporter(enabled=False)

    def _resolve_workdir(self, image: str, *, pullable: bool = True) -> str:
        """Where the repo lives inside `image` - its own WORKDIR if it declares a usable one.

        Prebuilt benchmark images ship the repo at their WORKDIR with dependencies installed
        against that exact path. Cloning a second copy into /workspace and patching that one is
        how a *correct* patch came to be graded as a failure: the patch landed in /workspace
        while Python imported the untouched copy the image had installed from /app. Nothing in
        the output said so - tests ran, results parsed, a verdict printed.

        Read from the image rather than hardcoded, so this needs no per-language or
        per-publisher table; "/" and "" are ignored as meaningless, and anything else falls
        back to DEFAULT_REPO_WORKDIR, which is what a plain base image gets.
        """
        declared = self.docker.image_workdir(image).strip()
        if not declared and not self.docker.image_exists(image):
            if not pullable:
                # Our own `evals/...` tag, which only `evals init` ever creates. It cannot be
                # pulled from a registry, so saying "try docker pull" would send the user down
                # a road that dead-ends in "pull access denied".
                raise RuntimeError(
                    f"image '{image}' does not exist locally.\n"
                    f"Build it first:  evals init <bundle>\n"
                    f"(Use --force if you need to rebuild an existing one.)"
                )
            # `docker inspect` only reads the LOCAL image store, and BuildKit (the default
            # builder) pulls base images into its own cache instead - so a base image can build
            # successfully and still be invisible to inspect. That silently returned "" here,
            # fell back to /workspace, and rebuilt the very two-copy layout this resolution
            # exists to prevent. Pull explicitly so the answer is based on the real image
            # rather than on whether something happened to populate the store earlier.
            self.report.step(f"pulling {image} to read its workdir")
            self.docker.pull(image)
            if not self.docker.image_exists(image):
                # Refuse to guess. Falling through to DEFAULT_REPO_WORKDIR here builds an
                # image that looks fine and grades wrongly: the patch lands in /workspace
                # while the tests import the copy the base image installed at its own
                # WORKDIR, so pass2pass stays green and fail2pass can never flip. That is
                # exactly what happened - a silent fallback after a failed pull produced an
                # image whose oracle run reported UNSOLVED for a known-correct patch.
                # A loud failure costs a retry; a silent one costs trust in every verdict.
                raise RuntimeError(
                    f"could not read the configuration of base image '{image}'.\n"
                    f"It is not in the local image store and `docker pull` did not put it "
                    f"there, so the directory its repo lives in is unknown.\n"
                    f"Building anyway would place the repo at {DEFAULT_REPO_WORKDIR} while "
                    f"the image's own tooling expects it elsewhere - patches would apply to "
                    f"a copy the tests never import, and results would be silently wrong.\n"
                    f"Fix: pull it yourself and re-run, e.g.\n"
                    f"  docker pull {image}\n"
                    f"then `evals init <bundle> --force`."
                )
            declared = self.docker.image_workdir(image).strip()
        if declared and declared not in ("/", ".") and declared.startswith("/"):
            return declared.rstrip("/")
        return DEFAULT_REPO_WORKDIR

    # ---------------------------------------------------------------- init

    def init(self, bundle_path: Path, force: bool = False) -> dict[str, Any]:
        bundle = TaskBundle(bundle_path)
        task = bundle.task
        tag = image_tag_for(task.task_id, task.commit)

        if not force and self.docker.image_exists(tag):
            logger.info("image %s already exists, skipping build", tag)
            return {
                "tag": tag, "built": False, "task_id": task.task_id,
                "commit": task.commit, "hidden_paths": list(task.hidden_paths),
            }

        # Said BEFORE the long silence starts, not after it ends. A first build of a
        # SWE-bench Pro bundle pulls several GB, and a user who has not been told that is
        # about to happen reasonably concludes the command has hung.
        print(f"Building {tag}")
        print(f"  base image: {task.base_image}")
        print("  first build of a benchmark image pulls several GB and can take 30+ minutes;")
        print("  it is cached afterwards, so later runs start in about a second.\n")

        # Resolved from the BASE image, so the build works inside whatever directory that
        # image already prepared. The built image then declares the same WORKDIR, which is how
        # validate/solve/grade recover it later without extra state.
        workdir = self._resolve_workdir(task.base_image)
        if workdir != DEFAULT_REPO_WORKDIR:
            logger.info("using the base image's declared workdir: %s", workdir)
        dockerfile_contents = render_dockerfile(task, workdir)
        with tempfile.TemporaryDirectory(prefix="evals-build-") as tmp:
            context = Path(tmp)
            dockerfile_path = context / "Dockerfile"
            _write_text(dockerfile_path, dockerfile_contents)
            exit_code, stdout, stderr = self.docker.build(
                context=context, tag=tag, dockerfile=dockerfile_path
            )

        if exit_code != 0:
            raise RuntimeError(f"docker build failed for {tag} (exit {exit_code}):\n{stderr}")

        return {
            "tag": tag, "built": True, "task_id": task.task_id, "commit": task.commit,
            "hidden_paths": list(task.hidden_paths),
            "stdout": stdout[-4000:], "stderr": stderr[-4000:],
        }

    # ---------------------------------------------------------------- container helpers

    def _start_container(self, tag: str, name: str, network: Optional[str] = None) -> None:
        exit_code, _, stderr = self.docker.run_detached(
            image=tag, name=name, command=["sleep", "infinity"],
            network_none=(network in (None, "none")),
            network=network if network not in (None, "none") else None,
        )
        if exit_code != 0:
            raise RuntimeError(f"failed to start container {name} from {tag} (exit {exit_code}):\n{stderr}")

    def _restore_guardrail_tests(self, container: str, bundle: TaskBundle, workdir: str) -> list[str]:
        """Restore both buckets' test files to their ORIGINAL repo paths in the container.

        Earlier versions staged each bucket into an isolated /workspace/_hidden/<bucket>/
        and ran them as two separate invocations. That only works for runners that can
        execute a test file from anywhere: Go and Java tests must sit in their package
        directory to compile at all, and JS tests using relative `require('../src/x')`
        break the moment they are moved. (Python was not immune either - a pytest file
        doing `from ..conftest import x` had the same problem.)

        Restoring in place is what SWE-bench's own harness does, and it is why the bucket
        directories mirror repo-relative paths. Bucket separation then happens at the
        *results* level instead of the execution level - see `_partition_cases`.

        Returns the repo-relative paths that were restored.
        """
        files = bundle.guardrail_files()
        if not files:
            raise RuntimeError(
                f"bundle {bundle.path} declares no guardrail test files in "
                f"tests/pass2pass/ or tests/fail2pass/"
            )

        # One tar stream over stdin rather than a docker cp per file: fewer round trips,
        # and it recreates intermediate directories for free.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for rel_path, local_file in sorted(files.items()):
                info = tarfile.TarInfo(name=rel_path)
                data = local_file.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))

        exit_code, _, stderr = self.docker.exec_stdin(
            container, ["sh", "-c", f"tar -x -C {workdir}"], buf.getvalue()
        )
        if exit_code != 0:
            raise RuntimeError(f"failed to restore guardrail tests into container: {stderr}")
        return sorted(files)

    def _apply_diff(self, container: str, diff_text: str, remote_path: str, workdir: str) -> tuple[bool, Optional[str]]:
        """Pipe a diff into the container over stdin and `git apply` it.

        stdin rather than a host temp file + `docker cp`: fewer moving parts, no host disk
        touchpoint, and it removes the text-mode write that caused CRLF corruption on
        Windows. SWE-bench's own harness does the same (`git apply -v -`).

        All diffs we handle are normalized to plain a/ b/ form, so -p1 always applies.
        """
        cp_exit, _, cp_stderr = self.docker.exec_stdin(
            container, ["sh", "-c", f"cat > {remote_path}"], diff_text.encode("utf-8")
        )
        if cp_exit != 0:
            return False, f"failed to write diff into container: {cp_stderr}"

        # A failed apply (e.g. a malformed LLM-produced diff) is an expected outcome we
        # report, not a docker malfunction.
        #
        # Tried as a ladder, strictest first, because rejecting a patch whose INTENT was
        # unambiguous makes the harness the reason a model scored zero. Each rung relaxes
        # exactly one thing and none of them can change what the patch means:
        #
        #   plain              the diff as written.
        #   --recount          recompute hunk line counts from the hunk body. Models are far
        #                      better at writing lines than counting them, and a miscount is
        #                      arithmetic, not intent.
        #   --ignore-whitespace  tolerate whitespace differences INSIDE a line. Tabs-vs-spaces
        #                      in a context line says nothing about the change being requested.
        #   -C1                require one line of surrounding context instead of three. This
        #                      is what rescues the common failure where a model drops a blank
        #                      line from its context block. It is the last rung because it is
        #                      the only one that weakens WHERE a hunk may land - the added and
        #                      removed lines must still match exactly, so a patch that is
        #                      simply wrong still cannot apply.
        #
        # What the ladder never does is guess at content. `git apply` remains the adjudicator;
        # we only stop it failing over bookkeeping the model got wrong on the way to a change
        # it expressed clearly. Aider reaches the same conclusion from the other direction -
        # "strive to be maximally flexible when interpreting the model's edit instructions" -
        # and gets there by making the applier a fuzzy search/replace.
        # The final rung is GNU patch with fuzz, which is what SWE-bench's own harness falls
        # back to (its GIT_APPLY_CMDS ends in `patch --batch --fuzz=5 -p1 -i`). It matches
        # hunks by content with a tolerance, so it survives drift that git apply will not -
        # at the cost of being able to land a hunk somewhere git would have refused. That
        # trade is why it is last and why its use is announced rather than silent.
        attempts = (
            f"git apply -p1 {remote_path}",
            f"git apply -p1 --recount {remote_path}",
            f"git apply -p1 --recount --ignore-whitespace {remote_path}",
            f"git apply -p1 --recount --ignore-whitespace -C1 {remote_path}",
            f"patch --batch --fuzz=5 -p1 -i {remote_path}",
        )
        apply_exit, apply_stderr, used = 1, "", ""
        for command in attempts:
            apply_exit, _, apply_stderr = self.docker.exec(
                container,
                ["sh", "-c", f"cd {workdir} && {command}"],
                warn_on_failure=False,
            )
            if apply_exit == 0:
                used = command
                break
            if command.startswith("patch "):
                # Every `git apply` rung is all-or-nothing, so a failed one leaves the tree
                # untouched. GNU patch is not: it applies the hunks it can, drops .rej files
                # for the rest, and still exits nonzero. Grading that half-applied state would
                # report a result the model never produced, so undo it.
                #
                # Restricted to the paths this diff names rather than `git checkout -- .`,
                # which would resurrect the pass2pass/fail2pass files that hidden_paths
                # deleted from the image - they are absent from the working tree but still
                # present in HEAD, so a blanket checkout would quietly undo the whole point
                # of hiding them.
                touched = diff_touched_paths(diff_text)
                specs = " ".join(shlex.quote(p) for p in touched) if touched else ""
                cleanup = "find . -name '*.rej' -o -name '*.orig' | xargs -r rm -f; true"
                if specs:
                    cleanup = f"git checkout -- {specs} 2>/dev/null; " + cleanup
                self.docker.exec(
                    container, ["sh", "-c", f"cd {workdir} && {cleanup}"], warn_on_failure=False
                )
        if apply_exit == 0 and used != attempts[0]:
            # Surfaced rather than silent: a patch that needed loosening is still a patch the
            # model got slightly wrong, and that is worth seeing in the run output.
            self.report.note(f"patch needed a fallback: {used.replace(remote_path, '<diff>')}")
        if apply_exit != 0:
            # `git apply` on its own reports only the first failing file and line. Re-run with
            # --check --verbose to get per-hunk detail (which hunk, at what offset, and whether
            # it was context mismatch vs a malformed hunk) - it inspects without touching the
            # tree, so it is safe to run after a failure. This is the difference between
            # "patch does not apply" and knowing which hunk to look at in the artifacts.
            _, _, verbose_stderr = self.docker.exec(
                container,
                ["sh", "-c", f"cd {workdir} && git apply --check --verbose -p1 {remote_path}"],
                warn_on_failure=False,
            )
            detail = (verbose_stderr or "").strip() or (apply_stderr or "").strip()
            return False, (
                f"diff did not apply cleanly (exit {apply_exit}), including after retrying with "
                f"--recount, --ignore-whitespace and reduced context. The content itself does "
                f"not match the file:\n{detail}"
            )
        return True, None

    def _capture_diff(self, container: str, workdir: str) -> str:
        """Diff a fresh .git-free copy of the workspace against the frozen build-time
        baseline. HEAD never moves, so the pinned commit stays the reproducibility anchor."""
        snapshot_exit, _, snapshot_stderr = self.docker.exec(
            container,
            ["sh", "-c",
             f"rm -rf {CURRENT_SNAPSHOT_DIR}/* {CURRENT_SNAPSHOT_DIR}/.[!.]* 2>/dev/null; "
             f"cp -a {workdir}/. {CURRENT_SNAPSHOT_DIR}/ && rm -rf {CURRENT_SNAPSHOT_DIR}/.git"],
        )
        if snapshot_exit != 0:
            raise RuntimeError(f"failed to snapshot solved workspace: {snapshot_stderr}")

        # git diff --no-index exits 0 (identical) or 1 (differences found) by `diff`
        # convention; only >=2 is a real error.
        diff_exit, diff_stdout, diff_stderr = self.docker.exec(
            container,
            ["sh", "-c", f"git diff --no-index --no-color {BASELINE_DIR} {CURRENT_SNAPSHOT_DIR}"],
            warn_on_failure=False,
        )
        if diff_exit not in (0, 1):
            raise RuntimeError(f"failed to capture diff in solve container (exit {diff_exit}): {diff_stderr}")
        return normalize_captured_diff(diff_stdout)

    def _reset_protected_paths(self, container: str, task: TaskSpec, workdir: str) -> None:
        """Undo any solver changes to test infrastructure before grading.

        Hiding the guardrail tests is necessary but not sufficient: the diff is applied
        wholesale, so a solver could influence grading through a root conftest.py,
        pytest.ini, sitecustomize.py, etc. SWE-bench's harness resets test files from git
        before running for exactly this reason; this is the same idea, scoped to
        task.json's protected_paths.
        """
        if not task.protected_paths:
            return
        specs = " ".join(shlex.quote(p) for p in task.protected_paths)
        # checkout restores tracked files the solver modified; clean removes ones it added.
        self.docker.exec(
            container,
            ["sh", "-c",
             f"cd {workdir} && git checkout HEAD -- {specs} 2>/dev/null; "
             f"git clean -fdq -- {specs} 2>/dev/null; true"],
            warn_on_failure=False,
        )

    def _run_test_buckets(
        self, container: str, task: TaskSpec, bundle: TaskBundle,
        test_paths: list[str], workdir: str, artifacts_dir: Optional[Path] = None,
    ) -> dict[str, dict[str, Any]]:
        """Run test_cmd ONCE over the restored test files, then split results per bucket.

        pass2pass and fail2pass usually live in the *same file*, so they cannot be executed
        separately once the files sit at their real repo paths. Running once and
        partitioning by test id is both simpler and what SWE-bench does - and it is what
        makes non-Python runners work, since nothing has to be relocated.

        test_cmd placeholders:
          {path}   space-separated repo-relative test file paths (pytest, jest)
          {dirs}   space-separated ./-prefixed parent directories (go test, package-based)
          {report} where to write JUnit XML; omitted means `--junitxml=` is appended
        """
        hidden_dir = f"{workdir}/_hidden"
        remote_xml_path = f"{hidden_dir}/results.xml"
        paths_arg = " ".join(shlex.quote(p) for p in test_paths)
        dirs_arg = " ".join(
            shlex.quote(f"./{d}") for d in sorted({str(PurePosixPath(p).parent) for p in test_paths})
        )

        fmt = {"path": paths_arg, "dirs": dirs_arg, "report": remote_xml_path}
        if "{report}" in task.test_cmd:
            full_command = task.test_cmd.format(**fmt)
        else:
            full_command = f"{task.test_cmd.format(**fmt)} --junitxml={remote_xml_path}"
        full_command = f"mkdir -p {hidden_dir} && cd {workdir} && {full_command}"

        # A nonzero exit just means some tests failed - expected, meaningful data that we
        # read from the XML, not a docker malfunction.
        test_exit_code, test_stdout, test_stderr = self.docker.exec(
            container, ["sh", "-c", full_command], warn_on_failure=False
        )

        with tempfile.TemporaryDirectory(prefix="evals-results-") as tmp:
            local_xml_path = Path(tmp) / "results.xml"
            cp_exit, _, cp_stderr = self.docker.cp_from(container, remote_xml_path, local_xml_path)
            if cp_exit != 0 or not local_xml_path.exists():
                raise RuntimeError(
                    f"no JUnit XML produced by the test command.\n"
                    f"  command:   {full_command}\n"
                    f"  exit code: {test_exit_code}\n"
                    f"  stdout:\n{test_stdout[-4000:]}\n  stderr:\n{test_stderr[-4000:]}\n{cp_stderr}\n"
                    f"The harness reads results from JUnit XML - make sure test_cmd writes it "
                    f"to the path given by the '{{report}}' placeholder (or is a pytest "
                    f"command, in which case --junitxml is appended automatically)."
                )
            cases = parse_junit_xml(local_xml_path)
            xml_bytes = local_xml_path.read_bytes()

        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "results.xml").write_bytes(xml_bytes)
            _write_text(
                artifacts_dir / "tests.log",
                f"$ {full_command}\n\n{test_stdout}\n{test_stderr}"[:MAX_STORED_LOG_CHARS],
            )

        return _partition_cases(cases, bundle, test_exit_code)

    # ---------------------------------------------------------------- validate

    def validate(
        self, bundle_path: Path, image: Optional[str] = None,
        artifacts_dir: Optional[Path] = None, network: Optional[str] = None,
    ) -> dict[str, Any]:
        bundle = TaskBundle(bundle_path)
        task = bundle.task
        tag = image or image_tag_for(task.task_id, task.commit)

        # Recovered from the BUILT image, which inherited the WORKDIR init chose. No extra
        # state to keep in sync - the image is the record of where its own repo lives.
        workdir = self._resolve_workdir(tag, pullable=False)
        container = f"evals-validate-{task.task_id}-{uuid.uuid4().hex[:8]}"
        self.report.step(f"starting validate container from {tag}")
        self._start_container(tag, container, network=network)
        try:
            self.report.step("restoring guardrail tests to their repo paths")
            test_paths = self._restore_guardrail_tests(container, bundle, workdir)
            self.report.step("running tests (compiled languages build first - this can take minutes)")
            raw_buckets = self._run_test_buckets(
                container, task, bundle, test_paths, workdir, artifacts_dir)
        finally:
            self.report.done()
            self.docker.rm(container, force=True)

        buckets = {
            bucket: {
                "ok": _bucket_passes_expectation(bucket, data["tests"], data["missing_tests"]),
                "exit_code": data["exit_code"],
                "tests": data["tests"],
                "missing_tests": data["missing_tests"],
            }
            for bucket, data in raw_buckets.items()
        }
        overall_ok = all(buckets[b]["ok"] for b in ALL_BUCKETS)
        expected_label = {BUCKET_PASS2PASS: "passed", BUCKET_FAIL2PASS: "not passed"}

        return {
            "bundle": str(bundle_path), "task_id": task.task_id, "commit": task.commit,
            "image": tag, "status": "passed" if overall_ok else "failed",
            "buckets": buckets,
            "table": render_bucket_table(buckets, expected_label),
        }

    # ---------------------------------------------------------------- solve

    def solve(
        self, bundle_path: Path, solver: str = "stub", image: Optional[str] = None,
        provider: Optional[str] = None, model: Optional[str] = None,
        network: Optional[str] = None, temperature: Optional[float] = None,
        thinking: Optional[str] = None, append_prompt: Optional[str] = None,
    ) -> SolveResult:
        """Container A: produce a patch. Never sees the hidden tests, never grades."""
        bundle = TaskBundle(bundle_path)
        task = bundle.task
        tag = image or image_tag_for(task.task_id, task.commit)
        patch_text = bundle.patch_path.read_text(encoding="utf-8")
        solver_instance = get_solver(
            solver, patch_text=patch_text, provider=provider, model=model,
            temperature=temperature, thinking=thinking, append_prompt=append_prompt,
        )

        workdir = self._resolve_workdir(tag, pullable=False)
        container = f"evals-run-solve-{task.task_id}-{uuid.uuid4().hex[:8]}"
        self.report.step(f"starting solve container from {tag}")
        self._start_container(tag, container, network=network)

        result = SolveResult()
        try:
            files = ContainerFiles(self.docker, container, workdir)
            description = bundle.description_path.read_text(encoding="utf-8")
            self.report.step(f"running {solver} solver")
            if solver == "llm":
                self.report.note("reading repo files and calling the model (no output until it replies)")
            try:
                diff_text = solver_instance.solve(files, description)
            except Exception as exc:
                # Carry the prompt/response out on the failure path too. A solve that raises
                # used to produce no artifacts at all, so the one thing needed to diagnose it -
                # what the model actually replied - was gone, and the only recourse was to
                # re-run and hope it reproduced. The metadata the solver recorded before
                # raising is attached to the exception's run so `--keep-artifacts` still writes
                # solver_prompt.txt and solver_raw_response.txt.
                meta = dict(getattr(solver_instance, "metadata", None) or {})
                # pop, mirroring the success path below: these two are large (a real prompt is
                # ~350KB) and belong in the artifacts directory, not inlined into the ledger row
                # that stores solver_metadata as JSON.
                result.prompt = meta.pop("prompt", None)
                result.raw_response = meta.pop("raw_response", None)
                result.solver_metadata = meta or None
                raise SolveFailure(result, exc) from exc
            result.raw_solver_diff = diff_text or ""

            if diff_text and diff_text.strip():
                self.report.step("applying the patch")
                result.patch_applied, result.patch_error = self._apply_diff(
                    container, diff_text, "/tmp/solver.diff", workdir
                )
                if result.patch_applied:
                    self.report.step("capturing the diff against the build-time baseline")
                    result.diff = self._capture_diff(container, workdir)
                else:
                    self.report.note("patch rejected; the graded repo will be unmodified")
            else:
                self.report.note("solver produced no changes")
        finally:
            self.report.done()
            self.docker.rm(container, force=True)

        metadata = dict(getattr(solver_instance, "metadata", None) or {})
        result.prompt = metadata.pop("prompt", None)
        result.raw_response = metadata.pop("raw_response", None)
        result.solver_metadata = metadata or None
        return result

    # ---------------------------------------------------------------- grade

    def grade(
        self, bundle_path: Path, diff: str, image: Optional[str] = None,
        artifacts_dir: Optional[Path] = None, network: Optional[str] = None,
    ) -> GradeResult:
        """Container B: a fresh container, the patch, the hidden tests, a verdict.

        Callable with any patch from any source - a solver, the ledger (`resume`/`replay`),
        or an external file (`grade --diff-file`).
        """
        bundle = TaskBundle(bundle_path)
        task = bundle.task
        tag = image or image_tag_for(task.task_id, task.commit)

        touched = [p for p in diff_touched_paths(diff) if _is_protected(p, task.protected_paths)]

        workdir = self._resolve_workdir(tag, pullable=False)
        container = f"evals-run-grade-{task.task_id}-{uuid.uuid4().hex[:8]}"
        self._start_container(tag, container, network=network)
        reapply_error: Optional[str] = None
        try:
            if diff.strip():
                reapplied, reapply_error = self._apply_diff(container, diff, "/tmp/patch.diff", workdir)
                if not reapplied:
                    raise RuntimeError(f"diff failed to apply in the grading container: {reapply_error}")
                self._reset_protected_paths(container, task, workdir)

            self.report.step("restoring guardrail tests to their repo paths")
            test_paths = self._restore_guardrail_tests(container, bundle, workdir)
            self.report.step("running tests (compiled languages build first - this can take minutes)")
            raw_buckets = self._run_test_buckets(
                container, task, bundle, test_paths, workdir, artifacts_dir)
        finally:
            self.report.done()
            self.docker.rm(container, force=True)

        p2p = raw_buckets[BUCKET_PASS2PASS]
        f2p = raw_buckets[BUCKET_FAIL2PASS]
        p2p_ok = bool(p2p["tests"]) and not p2p["missing_tests"] and all(
            c["outcome"] == "passed" for c in p2p["tests"])
        f2p_ok = bool(f2p["tests"]) and not f2p["missing_tests"] and all(
            c["outcome"] == "passed" for c in f2p["tests"])
        f2p_any = any(c["outcome"] == "passed" for c in f2p["tests"])

        if not p2p_ok:
            verdict = "REGRESSION"
        elif f2p_ok:
            verdict = "SOLVED"
        elif f2p_any:
            verdict = "PARTIAL"
        else:
            verdict = "UNSOLVED"

        buckets = {
            BUCKET_PASS2PASS: {"ok": p2p_ok, **{k: p2p[k] for k in ("exit_code", "tests", "missing_tests")}},
            BUCKET_FAIL2PASS: {"ok": f2p_ok, **{k: f2p[k] for k in ("exit_code", "tests", "missing_tests")}},
        }
        expected_label = {BUCKET_PASS2PASS: "passed", BUCKET_FAIL2PASS: "passed"}

        return GradeResult(
            verdict=verdict, buckets=buckets,
            table=render_bucket_table(buckets, expected_label),
            touched_protected_paths=touched, reapply_error=reapply_error,
        )

    # ---------------------------------------------------------------- run = solve + grade

    def run(
        self, bundle_path: Path, solver: str = "stub", image: Optional[str] = None,
        provider: Optional[str] = None, model: Optional[str] = None,
        artifacts_dir: Optional[Path] = None, network: Optional[str] = None,
    ) -> dict[str, Any]:
        """Convenience composition. `cli.py` calls solve/grade directly so it can persist
        the diff at the checkpoint in between."""
        solved = self.solve(bundle_path, solver, image, provider, model, network)
        graded = self.grade(bundle_path, solved.diff, image, artifacts_dir, network)
        return build_run_report(bundle_path, TaskBundle(bundle_path).task,
                                image or image_tag_for(TaskBundle(bundle_path).task.task_id,
                                                       TaskBundle(bundle_path).task.commit),
                                solver, solved, graded)


def _is_protected(path: str, protected: tuple[str, ...]) -> bool:
    """Match a diff path against protected pathspecs (git-style: * spans directories)."""
    from fnmatch import fnmatch
    name = path.rsplit("/", 1)[-1]
    for spec in protected:
        if fnmatch(path, spec) or fnmatch(name, spec.lstrip("*/")) or path == spec:
            return True
    return False


def build_run_report(
    bundle_path: Path, task: TaskSpec, tag: str, solver: str,
    solved: SolveResult, graded: GradeResult, source_run_id: Optional[int] = None,
) -> dict[str, Any]:
    """The structured evaluation artifact written to reports/ and the ledger."""
    return {
        "bundle": str(bundle_path),
        "task_id": task.task_id,
        "commit": task.commit,
        "image": tag,
        "solver": solver,
        "solver_metadata": solved.solver_metadata,
        "verdict": graded.verdict,
        "patch_applied": solved.patch_applied,
        "patch_error": solved.patch_error,
        "touched_protected_paths": graded.touched_protected_paths,
        "diff": solved.diff,
        "buckets": graded.buckets,
        "table": graded.table,
        "source_run_id": source_run_id,
    }
