"""Task bundle loading: task.json, description.md, patch.diff, tests/."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_TASK_JSON_FIELDS = ("task_id", "repo", "commit", "test_cmd", "deps_cmd")
_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]|^[/\\]")
# A Windows drive letter inside a command that runs *inside a Linux container* is
# almost always Git Bash / MSYS having rewritten a POSIX path on the way in.
_DRIVE_LETTER_RE = re.compile(r"[A-Za-z]:[/\\]")

DEFAULT_BASE_IMAGE = "python:3.11-slim"

# test_cmd must name what to run via at least one of these. `{report}` (the JUnit XML
# destination) is optional - if absent the runner appends `--junitxml=<path>`, which
# only makes sense for pytest, so non-pytest runners spell it out explicitly.
TEST_TARGET_PLACEHOLDERS = ("path", "dirs")

# Paths reset in the grading container after the solver's diff is applied, so a solver
# can't influence its own grade through test infrastructure (see DESIGN_NOTES.md §2).
# Deliberately conservative: only files whose *sole* purpose is test configuration.
# setup.cfg and pyproject.toml can also carry pytest settings, but legitimate fixes touch
# them (adding a dependency), so resetting them by default would break real solutions -
# add them to protected_paths per-task when a bundle warrants it.
DEFAULT_PROTECTED_PATHS = (
    "*conftest.py",
    "*pytest.ini",
    "*tox.ini",
    "*sitecustomize.py",
)
# Runs as root before the repo is cloned. MUST leave `git` on PATH (the harness uses
# it to clone, apply patches, and diff). Override for non-Debian / non-Python repos.
DEFAULT_SETUP_CMD = (
    "apt-get update "
    "&& apt-get install -y --no-install-recommends git "
    "&& rm -rf /var/lib/apt/lists/* "
    "&& pip install --no-cache-dir pytest"
)


class BundleError(ValueError):
    """Raised when a task bundle is missing required files, dirs, or task.json fields."""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    repo: str
    commit: str
    test_cmd: str
    deps_cmd: str
    # Repo-relative file paths removed from the image at `evals init` - the pass2pass
    # tests that already exist in the base repo, hidden so the solver can't see them.
    # fail2pass tests are typically new (not yet in the base repo), so they rarely
    # need an entry here. Everything else in the repo stays visible to the solver.
    hidden_paths: tuple[str, ...] = ()
    # Container image the task is built on. Change this (plus setup_cmd/deps_cmd/
    # test_cmd) to support non-Python repos - nothing else in the harness is
    # language-specific, since results are read from JUnit XML.
    base_image: str = DEFAULT_BASE_IMAGE
    # Root-level image prep, before the repo is cloned. Must install git.
    setup_cmd: str = DEFAULT_SETUP_CMD
    # Test-infrastructure paths reset before grading (anti-tamper).
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS


class TaskBundle:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.task_json_path = self.path / "task.json"
        self.description_path = self.path / "description.md"
        self.patch_path = self.path / "patch.diff"
        self.pass2pass_dir = self.path / "tests" / "pass2pass"
        self.fail2pass_dir = self.path / "tests" / "fail2pass"

        self._validate_structure()
        self.task = self._load_task_json()

    # Files inside a bucket that describe it rather than being tests themselves.
    BUCKET_METADATA_FILES = ("_selected_tests.txt",)

    def guardrail_files(self) -> dict[str, Path]:
        """{repo_relative_path: local file} for every guardrail test in both buckets.

        Bucket directories mirror repo-relative paths (e.g.
        tests/pass2pass/test/units/utils/test_vars.py), because the runner restores these
        files to their *original* locations in the container - Go/Java tests need their
        package directory to compile, and JS relative imports break when files move.
        The same file legitimately appears in both buckets; last one wins, contents match.
        """
        files: dict[str, Path] = {}
        for bucket_dir in (self.pass2pass_dir, self.fail2pass_dir):
            for path in sorted(bucket_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(bucket_dir)
                if rel.name in self.BUCKET_METADATA_FILES:
                    continue
                files[rel.as_posix()] = path
        return files

    def _validate_structure(self) -> None:
        if not self.path.exists():
            raise BundleError(f"Task bundle directory not found: {self.path}")
        if not self.path.is_dir():
            raise BundleError(f"Task bundle path is not a directory: {self.path}")

        missing: list[str] = []
        if not self.task_json_path.is_file():
            missing.append(f"task.json (expected at {self.task_json_path})")
        if not self.description_path.is_file():
            missing.append(f"description.md (expected at {self.description_path})")
        if not self.patch_path.is_file():
            missing.append(f"patch.diff (expected at {self.patch_path})")
        if not self.pass2pass_dir.is_dir():
            missing.append(f"tests/pass2pass/ directory (expected at {self.pass2pass_dir})")
        if not self.fail2pass_dir.is_dir():
            missing.append(f"tests/fail2pass/ directory (expected at {self.fail2pass_dir})")

        if missing:
            details = "\n".join(f"  - {item}" for item in missing)
            raise BundleError(
                f"Task bundle at {self.path} is missing required file(s)/dir(s):\n{details}"
            )

    def _load_task_json(self) -> TaskSpec:
        try:
            raw_text = self.task_json_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BundleError(f"Could not read {self.task_json_path}: {exc}") from exc

        try:
            data: Any = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise BundleError(
                f"task.json at {self.task_json_path} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise BundleError(
                f"task.json at {self.task_json_path} must contain a JSON object, "
                f"got {type(data).__name__}"
            )

        missing_fields = [f for f in REQUIRED_TASK_JSON_FIELDS if f not in data]
        if missing_fields:
            raise BundleError(
                f"task.json at {self.task_json_path} is missing required field(s): "
                f"{', '.join(missing_fields)}"
            )

        empty_fields = [f for f in REQUIRED_TASK_JSON_FIELDS if not str(data[f]).strip()]
        if empty_fields:
            raise BundleError(
                f"task.json at {self.task_json_path} has empty value(s) for required "
                f"field(s): {', '.join(empty_fields)}"
            )

        hidden_paths = self._parse_hidden_paths(data.get("hidden_paths"))
        protected_paths = (
            self._parse_path_list(data["protected_paths"], "protected_paths", allow_globs=True)
            if data.get("protected_paths") is not None
            else DEFAULT_PROTECTED_PATHS
        )
        test_cmd = str(data["test_cmd"])
        deps_cmd = str(data["deps_cmd"])
        setup_cmd = str(data.get("setup_cmd") or DEFAULT_SETUP_CMD)
        base_image = str(data.get("base_image") or DEFAULT_BASE_IMAGE).strip() or DEFAULT_BASE_IMAGE

        # These three run inside a Linux container, so a host drive letter is a bug -
        # usually Git Bash rewriting a /container/path argument on its way in.
        for field, value in (("test_cmd", test_cmd), ("deps_cmd", deps_cmd), ("setup_cmd", setup_cmd)):
            if _DRIVE_LETTER_RE.search(value):
                raise BundleError(
                    f"task.json at {self.task_json_path} field '{field}' contains a Windows-style "
                    f"path, which cannot exist inside the Linux container:\n  {value}\n"
                    f"If you passed this on the command line from Git Bash / MSYS, it rewrote your "
                    f"POSIX path. Re-run with MSYS_NO_PATHCONV=1, use PowerShell, or edit task.json "
                    f"directly."
                )

        if not any(f"{{{name}}}" in test_cmd for name in TEST_TARGET_PLACEHOLDERS):
            raise BundleError(
                f"task.json at {self.task_json_path} field 'test_cmd' must contain at least "
                f"one test-target placeholder, so the harness can tell the runner which "
                f"tests to execute:\n"
                f"  {{path}}  - space-separated repo-relative test FILES (pytest, jest)\n"
                f"  {{dirs}}  - the ./-prefixed parent DIRECTORIES of those files "
                f"(go test, which takes packages)\n"
                f"Got: {test_cmd}"
            )

        return TaskSpec(
            task_id=str(data["task_id"]),
            repo=str(data["repo"]),
            commit=str(data["commit"]),
            test_cmd=test_cmd,
            deps_cmd=deps_cmd,
            hidden_paths=hidden_paths,
            base_image=base_image,
            setup_cmd=setup_cmd,
            protected_paths=protected_paths,
        )

    def _parse_hidden_paths(self, raw: Any) -> tuple[str, ...]:
        if raw is None:
            return ()
        return self._parse_path_list(raw, "hidden_paths", allow_globs=False)

    def _parse_path_list(self, raw: Any, field: str, allow_globs: bool) -> tuple[str, ...]:
        if not isinstance(raw, list):
            raise BundleError(
                f"task.json at {self.task_json_path} field '{field}' must be a list "
                f"of repo-relative paths, got {type(raw).__name__}"
            )

        cleaned: list[str] = []
        malformed: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                malformed.append(repr(item))
                continue
            path = item.strip().replace("\\", "/")
            has_traversal = any(part == ".." for part in path.split("/"))
            if _ABSOLUTE_PATH_RE.match(path) or has_traversal:
                malformed.append(item)
                continue
            cleaned.append(path)

        if malformed:
            raise BundleError(
                f"task.json at {self.task_json_path} has malformed '{field}' "
                f"entr{'y' if len(malformed) == 1 else 'ies'}: {', '.join(malformed)} "
                f"(each must be a non-empty, relative, repo-relative path with no "
                f"'..' segments{'; globs like *conftest.py are allowed' if allow_globs else ''})"
            )

        return tuple(cleaned)
