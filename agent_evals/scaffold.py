"""Scaffold a task bundle from a SWE-bench (Pro) dataset row.

Maps dataset fields onto our bundle format:

  repo + base_commit      -> task.json repo/commit
  problem_statement (+ requirements/interface) -> description.md
  patch                   -> patch.diff (golden patch)
  test_patch + fail_to_pass / pass_to_pass ids -> tests/fail2pass/, tests/pass2pass/
  test file repo paths    -> task.json hidden_paths (stripped from the image at init)

Bucket directories mirror repo-relative paths, because the runner restores the
guardrail tests to their ORIGINAL locations before grading (Go/Java tests need
their package directory to compile; JS relative imports break if files move).
SWE-bench selects individual test *ids* and the same file often holds both
pass2pass and fail2pass tests, so buckets are separated by partitioning the
results of one test run against _selected_tests.txt - not by running twice.
"""

from __future__ import annotations

import ast
import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from agent_evals.bundle import BundleError

DEFAULT_DEPS_CMD = "pip install -e ."
DEFAULT_TEST_CMD = "pytest {path} -q"
DEFAULT_DATASET = "ScaleAI/SWE-bench_Pro"
DEFAULT_SPLIT = "test"

# Upper bound on rows scanned while looking for instance_id, so a typo'd id fails in
# seconds rather than silently streaming the entire split.
_MAX_ROWS_SCANNED = 20_000


def fetch_dataset_row(
    instance_id: str, dataset: str = DEFAULT_DATASET, split: str = DEFAULT_SPLIT,
) -> dict[str, Any]:
    """Fetch exactly one row from a HuggingFace dataset by its unique instance_id.

    Uses the official `datasets` library in streaming mode, so it never downloads the
    full dataset - just reads forward through the split until instance_id matches, then
    stops. This is the same library/approach HF's own docs recommend for exactly this
    lookup; datasets-server's REST `/filter` endpoint was tried first but 422s for this
    dataset (it only indexes datasets under 5GB, and SWE-bench Pro's test split doesn't
    qualify), so streaming is both simpler and the one that actually works.
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise BundleError(
            "The 'datasets' package is not installed, which --instance-id needs to fetch "
            "a row from HuggingFace. Run: pip install -e \".[scaffold]\" "
            "(or pass --row with a manually saved JSON row instead)."
        ) from exc

    rows = load_dataset(dataset, split=split, streaming=True)
    for i, row in enumerate(rows):
        if row.get("instance_id") == instance_id:
            return row
        if i + 1 >= _MAX_ROWS_SCANNED:
            break

    raise BundleError(
        f"No row with instance_id={instance_id!r} found in the first "
        f"{min(i + 1, _MAX_ROWS_SCANNED)} rows of {dataset}/{split}. Check the id in the "
        f"dataset viewer (huggingface.co/datasets/{dataset}), or use --row with a manually "
        f"saved JSON row instead."
    )

# Per-language starting points for the four execution knobs. These are *defaults to tune*,
# not guarantees - real repos differ - but they save the author from writing a Dockerfile.
# Every test_cmd must end up producing JUnit XML; where a runner needs a reporter package,
# setup_cmd installs it.
_APT_GIT = ("apt-get update && apt-get install -y --no-install-recommends git "
            "&& rm -rf /var/lib/apt/lists/*")

LANGUAGE_PROFILES: dict[str, dict[str, str]] = {
    "python": {
        "base_image": "python:3.11-slim",
        # Idempotent, because it is also appended to a dataset image that may already
        # have a pinned pytest we must not silently upgrade.
        "reporter_cmd": "python -c 'import pytest' 2>/dev/null || pip install --no-cache-dir pytest",
        "deps_cmd": "pip install -e .",
        "test_cmd": "pytest {path} -q",
    },
    "js": {
        "base_image": "node:20",
        "reporter_cmd": "npm ls -g jest-junit >/dev/null 2>&1 || npm install -g jest-junit",
        "deps_cmd": "npm ci || npm install",
        "test_cmd": ("JEST_JUNIT_OUTPUT_FILE={report} npx --yes jest {path} "
                     "--reporters=default --reporters=jest-junit"),
    },
    "go": {
        "base_image": "golang:1.22",
        "reporter_cmd": ("command -v go-junit-report >/dev/null 2>&1 "
                         "|| go install github.com/jstemmer/go-junit-report/v2@latest"),
        "deps_cmd": "go mod download",
        # {dirs}, not {path}: `go test` takes package directories, and a Go test file
        # cannot be compiled in isolation from the package it belongs to.
        "test_cmd": "go test -v {dirs} 2>&1 | go-junit-report -set-exit-code > {report}",
    },
}
for _profile in LANGUAGE_PROFILES.values():
    _profile["setup_cmd"] = f"{_APT_GIT} && {_profile['reporter_cmd']}"

# SWE-bench Pro publishes a prebuilt image per instance, named by the row's
# `dockerhub_tag`. It already contains the repo's toolchain and installed dependencies,
# which sidesteps the hardest part of authoring a bundle for an unfamiliar repo.
DATASET_IMAGE_NAMESPACE = "jefzda/sweap-images"
# Those images carry the repo's toolchain and dependencies, but nothing the *harness*
# needs: git (clone / apply / diff) and a JUnit reporter for the language. Neither is
# something the benchmark image had any reason to include - the teleport image has the
# Go toolchain and a warm module cache but no go-junit-report - so both are added here.
DATASET_IMAGE_GIT_CMD = (
    "command -v git >/dev/null 2>&1 || "
    "(apt-get update && apt-get install -y --no-install-recommends git "
    "&& rm -rf /var/lib/apt/lists/*)"
)

LANGUAGE_PROFILES["ts"] = LANGUAGE_PROFILES["js"]
LANGUAGE_PROFILES["typescript"] = LANGUAGE_PROFILES["js"]
LANGUAGE_PROFILES["javascript"] = LANGUAGE_PROFILES["js"]
LANGUAGE_PROFILES["py"] = LANGUAGE_PROFILES["python"]

def _parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    text = str(value)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return ast.literal_eval(text)


def _parse_text(value: Any) -> str:
    """Some dataset text fields arrive JSON-encoded (wrapped in quotes with \\n escapes)."""
    text = str(value)
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _run_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise BundleError(f"git {' '.join(args)} failed (exit {proc.returncode}):\n{proc.stderr}")


def _write_utf8(path: Path, content: str) -> None:
    # newline="" so diffs and test files keep exact \n bytes on Windows.
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb")


def _looks_like_a_path(test_id: str) -> bool:
    """Does this dataset test id start with a file path we can locate in the repo?

    pytest ids do (`test/units/x_test.py::Class::test`). Go ids do not - they are test
    function names, and crucially they use `/` as the *subtest* separator
    (`TestHTTPConnStateReporter/without_client_certs`), so the presence of a slash proves
    nothing. Requiring a real source-file suffix is what actually distinguishes them.
    JS ids vary by runner and are often messy (`a.js | describe... b.js::case`), so they
    are rejected here too and resolved from `before_repo_set_cmd` instead.
    """
    head = test_id.split("::", 1)[0].strip()
    if any(ch in head for ch in "| \t"):
        return False
    return head.endswith(_SOURCE_SUFFIXES)


# `before_repo_set_cmd` ends with the dataset's own test-staging step, e.g.
#   git checkout <fix-sha> -- lib/kube/proxy/forwarder_test.go
# which names the guardrail test files explicitly. This is the language-independent
# source of truth: it works for Go and JS rows whose test ids carry no path at all.
_GIT_CHECKOUT_PATHS_RE = re.compile(r"^\s*git\s+checkout\s+\S+\s+--\s+(?P<paths>.+?)\s*$", re.MULTILINE)


def guardrail_paths_from_setup_cmd(before_repo_set_cmd: str) -> list[str]:
    """Extract the test file paths the dataset stages via `git checkout <sha> -- <paths>`."""
    paths: list[str] = []
    for match in _GIT_CHECKOUT_PATHS_RE.finditer(before_repo_set_cmd or ""):
        for token in match.group("paths").split():
            token = token.strip().strip("'\"")
            if token and not token.startswith("-"):
                paths.append(token)
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(paths))


def resolve_test_files(
    ids: list[str], before_repo_set_cmd: str, language: str
) -> list[str]:
    """Repo-relative test files for a bucket, preferring ids and falling back to setup_cmd."""
    if ids and _looks_like_a_path(ids[0]):
        return sorted({tid.split("::", 1)[0] for tid in ids})

    fallback = guardrail_paths_from_setup_cmd(before_repo_set_cmd)
    if fallback:
        return fallback

    raise BundleError(
        f"Could not determine which test files this row's guardrail tests live in.\n"
        f"  language:        {language or 'unknown'}\n"
        f"  example test id: {(ids[0] if ids else '')!r}\n\n"
        f"Test ids carry a file path for pytest-style rows; for other runners the paths "
        f"normally come from the dataset's `before_repo_set_cmd` "
        f"(`git checkout <sha> -- <test files>`), but that field had none either.\n"
        f"Write this bundle by hand: create task.json / description.md / patch.diff, copy "
        f"the guardrail tests into tests/pass2pass/ and tests/fail2pass/, and set "
        f"base_image / setup_cmd / deps_cmd / test_cmd for the language. Everything after "
        f"scaffolding - init, validate, run, grading - is already language-agnostic."
    )


def scaffold_bundle(
    row: dict[str, Any],
    out_dir: Path,
    deps_cmd: Optional[str] = None,
    test_cmd: Optional[str] = None,
    task_id: str | None = None,
    use_dataset_image: bool = True,
) -> dict[str, Any]:
    repo = str(row["repo"])
    repo_url = repo if repo.startswith("http") else f"https://github.com/{repo}.git"
    commit = str(row["base_commit"])
    golden_patch = _parse_text(row["patch"])
    test_patch = _parse_text(row["test_patch"])
    fail_to_pass = _parse_list(row["fail_to_pass"])
    pass_to_pass = _parse_list(row["pass_to_pass"])
    task_id = task_id or out_dir.name

    if out_dir.exists() and any(out_dir.iterdir()):
        raise BundleError(f"output directory {out_dir} already exists and is not empty")

    buckets = {"fail2pass": fail_to_pass, "pass2pass": pass_to_pass}
    for bucket, ids in buckets.items():
        if not ids:
            raise BundleError(f"dataset row has no {bucket} test ids")

    # Pick language-appropriate execution knobs, honouring explicit CLI overrides.
    language = str(row.get("repo_language") or "").strip().lower()
    profile = LANGUAGE_PROFILES.get(language, LANGUAGE_PROFILES["python"])
    before_set_cmd = _parse_text(row.get("before_repo_set_cmd") or "")

    # The dataset ships a prebuilt image per instance with the full toolchain and
    # dependencies already installed. Using it removes the single most fragile part of
    # bundle authoring - guessing deps_cmd - and works identically for Go/JS/Python.
    dockerhub_tag = str(row.get("dockerhub_tag") or "").strip()
    if use_dataset_image and dockerhub_tag:
        base_image = f"{DATASET_IMAGE_NAMESPACE}:{dockerhub_tag}"
        setup_cmd = f"{DATASET_IMAGE_GIT_CMD} && ({profile['reporter_cmd']})"
        deps_cmd = deps_cmd or "true"  # already installed in the prebuilt image
    else:
        base_image = profile["base_image"]
        setup_cmd = profile["setup_cmd"]
        deps_cmd = deps_cmd or profile["deps_cmd"]
    test_cmd = test_cmd or profile["test_cmd"]

    # File paths for each bucket's guardrail tests. pytest ids embed the path; Go/JS ids
    # don't, so those fall back to the paths named in before_repo_set_cmd.
    files_per_bucket = {
        bucket: resolve_test_files(ids, before_set_cmd, language)
        for bucket, ids in buckets.items()
    }
    hidden_paths = sorted({p for paths in files_per_bucket.values() for p in paths})

    with tempfile.TemporaryDirectory(prefix="evals-scaffold-") as tmp:
        clone = Path(tmp) / "repo"
        clone.mkdir()
        # Fetch just the pinned commit (GitHub allows fetching arbitrary SHAs).
        _run_git(["init", "-q"], clone)
        _run_git(["remote", "add", "origin", repo_url], clone)
        _run_git(["fetch", "-q", "--depth", "1", "origin", commit], clone)
        _run_git(["checkout", "-q", "FETCH_HEAD"], clone)

        # Apply the dataset's test_patch: bucket files must reflect the *post-test_patch*
        # state (that is the version SWE-bench grades against).
        patch_file = Path(tmp) / "test.patch"
        _write_utf8(patch_file, test_patch if test_patch.endswith("\n") else test_patch + "\n")
        _run_git(["apply", str(patch_file)], clone)

        for bucket, ids in buckets.items():
            bucket_dir = out_dir / "tests" / bucket
            bucket_dir.mkdir(parents=True)
            for rel in files_per_bucket[bucket]:
                src = clone / rel
                if not src.is_file():
                    raise BundleError(
                        f"test file {rel} (from {bucket} ids) not found in repo after test_patch"
                    )
                # Mirror the repo-relative path, not just the basename: the runner restores
                # these files to their ORIGINAL locations so Go/Java tests keep their
                # package and JS relative imports keep resolving.
                dst = bucket_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)

            # The manifest is how results get attributed to a bucket after a single test
            # run - the same file routinely holds both pass2pass and fail2pass tests, so
            # they cannot be separated by execution. Only the trailing test name is used
            # for matching; the rest is kept for readability.
            selected = []
            for tid in ids:
                head, sep, rest = tid.partition("::")
                selected.append(f"{Path(head).name}::{rest}" if sep else tid)
            _write_utf8(bucket_dir / "_selected_tests.txt", "\n".join(selected) + "\n")

    description_parts = ["# " + task_id, "", _parse_text(row["problem_statement"]).strip()]
    for field, heading in (("requirements", "Requirements"), ("interface", "Interface")):
        if row.get(field):
            description_parts += ["", f"## {heading}", "", _parse_text(row[field]).strip()]

    task_json = {
        "task_id": task_id,
        "repo": repo_url,
        "commit": commit,
        "base_image": base_image,
        "setup_cmd": setup_cmd,
        "deps_cmd": deps_cmd,
        "test_cmd": test_cmd,
        "hidden_paths": hidden_paths,
    }
    _write_utf8(out_dir / "task.json", json.dumps(task_json, indent=2) + "\n")
    _write_utf8(out_dir / "description.md", "\n".join(description_parts) + "\n")
    _write_utf8(out_dir / "patch.diff", golden_patch if golden_patch.endswith("\n") else golden_patch + "\n")
    _write_utf8(out_dir / "source_row.json", json.dumps(row, indent=2) + "\n")

    return {
        "task_id": task_id,
        "out_dir": str(out_dir),
        "repo": repo_url,
        "commit": commit,
        "hidden_paths": hidden_paths,
        "fail2pass_tests": len(fail_to_pass),
        "pass2pass_tests": len(pass_to_pass),
    }
