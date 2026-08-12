"""Typer CLI entry point: evals init | evals validate | evals run | evals logs | evals history."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from agent_evals.bundle import BundleError, TaskBundle
from agent_evals.scaffold import DEFAULT_DATASET, DEFAULT_SPLIT
from agent_evals.db import (
    STATUS_ERROR, STATUS_PATCH_CAPTURED, STATUS_SUCCESS, RunDB,
)
from agent_evals.report import Report
from agent_evals.runner import (
    Runner, SolveFailure, SolveResult, StepReporter, build_run_report, image_tag_for,
)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a .env file (e.g. GEMINI_API_KEY=...) into the environment.

    Convenience only, not a requirement - every provider works fine with a plain `export`/
    `$env:` too. An already-exported variable wins, so `.env` is a fallback default rather
    than an override. Silently does nothing if no .env file exists. Stdlib only - not worth a
    dependency (python-dotenv) for ~15 lines.

    When the two disagree the shadowing is announced, because staying silent about it cost
    real debugging time: a shell still had an old key exported from a previous session, a new
    key was put in .env, and the run failed with "credentials rejected" while .env looked
    correct. Precedence is fine; hiding which value actually won is not.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        existing = os.environ.get(key)
        if existing is None:
            os.environ[key] = value
        elif existing != value:
            print(
                # ASCII only: this prints to a Windows console that is often cp1252, where a
                # stray non-ASCII separator renders as a replacement character.
                f"note: {key} is already set in your shell, so the different value in "
                f"{path} is being ignored. To use the {path} value, unset it first "
                f"(PowerShell: Remove-Item Env:\\{key} | bash: unset {key})."
            )


_load_dotenv()

REPORTS_DIR = Path("reports")
ARTIFACTS_DIR = Path("artifacts")

app = typer.Typer(
    name="evals",
    help="Run and grade LLM solvers against containerized SWE-bench-style coding tasks.",
    no_args_is_help=True,
)


# ------------------------------------------------------------------ shared helpers

def _task_id_for(bundle: Path) -> str:
    return TaskBundle(bundle).task.task_id


def graded_image(image: Optional[str], bundle: Path) -> str:
    if image:
        return image
    task = TaskBundle(bundle).task
    return image_tag_for(task.task_id, task.commit)


def _write_solve_artifacts(artifacts_dir: Optional[Path], solved: SolveResult) -> None:
    """Persist the bulky solve-phase material to disk (kept out of the DB row)."""
    if artifacts_dir is None:
        return
    from agent_evals.runner import _write_text

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("solver_prompt.txt", solved.prompt),
        ("solver_raw_response.txt", solved.raw_response),
        ("solver_raw_diff.diff", solved.raw_solver_diff),
        ("captured.diff", solved.diff),
    ):
        if text:
            _write_text(artifacts_dir / name, text)


def _print_run_result(result: dict, run_id: int, report_dir_note: Optional[Path] = None) -> None:
    print(result["table"])
    print(f"\nVerdict: {result['verdict']}")
    if result.get("patch_error"):
        # The patch failing to apply is the single most common way an LLM run goes wrong, and
        # the verdict alone ("UNSOLVED") hides the reason entirely - the tests below were run
        # against an UNCHANGED repo. Say that outright and point at the files that explain it.
        print(f"\nPatch error: {result['patch_error']}")
        print(
            "  -> The solver's diff was rejected, so the graded repo is unmodified: the "
            "verdict above reflects the baseline, not the solver's attempt."
        )
        if report_dir_note is not None:
            print(
                f"  -> Inspect {report_dir_note}: solver_raw_response.txt (what the model "
                f"actually returned) and solver_raw_diff.diff (the diff we tried to apply)."
            )
        else:
            print("  -> Re-run with --keep-artifacts to capture the model's raw response.")
    if result.get("touched_protected_paths"):
        print(
            "WARNING: the patch modified test infrastructure "
            f"({', '.join(result['touched_protected_paths'])}). "
            "Those paths were reset before grading, so the verdict is unaffected - but a "
            "solver touching them is worth investigating."
        )
    meta = result.get("solver_metadata")
    if meta and meta.get("provider"):
        print(
            f"LLM: provider={meta['provider']} model={meta['model']} "
            f"latency={meta.get('latency_seconds')}s "
            f"prompt~{meta.get('prompt_tokens_estimate')}tok "
            f"response={meta.get('response_chars')}ch "
            f"temp={meta.get('temperature')} thinking={meta.get('thinking')}"
        )
    if report_dir_note is None and result["verdict"] != "SOLVED" and not result.get("patch_error"):
        # The flag defaults off, and this is the case it hurts: a patch that applied cleanly
        # but did the wrong thing leaves a table of failures and nothing to look at. Say so at
        # the moment it matters rather than only in the README - the alternative is paying for
        # the inference call a second time just to see what the model said.
        print(
            "\nNo artifacts were kept for this run. Re-run with --keep-artifacts to save the "
            "solver prompt, its raw response, the diffs, the JUnit XML and the test log under "
            "artifacts/run-<id>/."
        )


def _read_append_prompt(value: Optional[str]) -> Optional[str]:
    """Resolve --append-prompt, which may be literal text or @path/to/file.

    Deliberately append-only. The bundle's description, the repo files and the output-format
    rules are what make a run comparable to any other run of the same task; letting a flag
    replace them would produce numbers that look like benchmark results but measure a
    different task. Appending lets you add guidance without touching what is being measured.
    """
    if not value:
        return None
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def _grade_stored_diff(
    run_id: Optional[int], as_new_run: bool, keep_artifacts: bool, bundle: Optional[str] = None
) -> None:
    """Shared implementation of `resume` and `replay`: grade a diff already in the ledger."""
    runner = Runner()
    with RunDB() as db:
        if run_id is None:
            # Default to the obvious run rather than making the user go find an id. Announced,
            # never silent - picking a run on someone's behalf and not saying which would make
            # the result impossible to interpret.
            run_id = db.latest_run_id(with_diff=True, task_like=bundle)
            if run_id is None:
                scope = f" for a bundle matching '{bundle}'" if bundle else ""
                print(
                    f"No run with a stored patch found{scope}. Run `evals run <bundle>` first, "
                    f"or see `evals history` for what exists."
                )
                raise typer.Exit(code=1)
            print(f"Using run {run_id} (most recent with a stored patch).")
        source = db.get_run(run_id)
        if source is None:
            print(f"No run found with run_id={run_id}")
            raise typer.Exit(code=1)

        diff_text = source.get("captured_diff")
        if not diff_text:
            print(
                f"Run {run_id} has no stored diff (status={source.get('status')}). "
                f"Only runs that reached {STATUS_PATCH_CAPTURED} can be resumed or replayed."
            )
            raise typer.Exit(code=1)

        bundle = Path(source["args"]["bundle"])
        image = source["args"].get("image")

        if as_new_run:
            target_id = db.create_run(
                command="replay", task_id=str(bundle),
                args={"bundle": str(bundle), "source_run_id": run_id, "image": image},
            )
            db.update_run(target_id, source_run_id=run_id, captured_diff=diff_text)
        else:
            target_id = run_id

        artifacts_dir = ARTIFACTS_DIR / f"run-{target_id}" if keep_artifacts else None
        try:
            graded = runner.grade(bundle, diff_text, image=image, artifacts_dir=artifacts_dir)
        except (BundleError, RuntimeError) as exc:
            log = f"{'replay' if as_new_run else 'resume'} failed: {exc}"
            print(log)
            db.finish_run(target_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        task = TaskBundle(bundle).task
        solved = SolveResult(
            diff=diff_text, patch_applied=bool(source.get("patch_applied")),
            patch_error=source.get("patch_error"),
        )
        solver_name = source.get("solver_name") or "stored"
        result = build_run_report(
            bundle, task, graded_image(image, bundle), solver_name, solved, graded,
            source_run_id=run_id if as_new_run else None,
        )
        _print_run_result(result, target_id)

        suffix = "replay" if as_new_run else "resume"
        report_path = REPORTS_DIR / f"{task.task_id}-{suffix}-{target_id}.json"
        Report(report_path).write(result)
        print(f"\nWrote report to {report_path}  (run_id={target_id}, source_run_id={run_id})")

        db.finish_run(
            target_id, status=STATUS_SUCCESS, results=result, verdict=result["verdict"],
            log=f"{suffix}: verdict={result['verdict']} from run {run_id} (no solver invoked)",
        )
        if result["verdict"] != "SOLVED":
            raise typer.Exit(code=1)


@app.command()
def init(
    bundle: Path = typer.Argument(..., help="Path to the task bundle directory."),
    force: bool = typer.Option(False, "--force", help="Rebuild even if the image already exists."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Also narrate the harness's own steps (workdir resolution, base-image pull). "
        "Docker's build output is streamed either way.",
    ),
) -> None:
    """Build a Docker image of the repo at task.json's commit, with deps installed and a non-root user."""
    args = {"bundle": str(bundle), "force": force}
    runner = Runner(reporter=StepReporter(enabled=verbose))
    with RunDB() as db:
        run_id = db.create_run(command="init", task_id=str(bundle), args=args)
        try:
            result = runner.init(bundle, force=force)
        except (BundleError, RuntimeError) as exc:
            log = f"init failed: {exc}"
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        if result["built"]:
            log = f"init: built image {result['tag']} (task_id={result['task_id']} commit={result['commit']})"
        else:
            log = f"init: image {result['tag']} already exists, skipped build"

        if result["hidden_paths"]:
            log += f"\nhidden_paths stripped from image: {', '.join(result['hidden_paths'])}"
        else:
            log += "\nhidden_paths: none declared in task.json"

        print(log)
        db.finish_run(run_id, status=STATUS_SUCCESS, results=result, log=log)


@app.command()
def validate(
    bundle: Path = typer.Argument(..., help="Path to the task bundle directory."),
    image: Optional[str] = typer.Option(
        None, help="Docker image to validate against (defaults to the image built by `evals init`)."
    ),
    keep_artifacts: bool = typer.Option(
        False, "--keep-artifacts", help="Save JUnit XML + test logs under artifacts/run-<id>/."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Print each step as it happens (container start, test restore, test run) with "
        "elapsed times. Worth using on compiled languages, where the test command builds "
        "the package first and can look hung for minutes.",
    ),
) -> None:
    """Run pass2pass (must all pass) and fail2pass (must all fail) on the baseline in a container."""
    args = {"bundle": str(bundle), "image": image, "keep_artifacts": keep_artifacts}
    runner = Runner(reporter=StepReporter(enabled=verbose))
    with RunDB() as db:
        run_id = db.create_run(command="validate", task_id=str(bundle), args=args)
        artifacts_dir = ARTIFACTS_DIR / f"run-{run_id}" if keep_artifacts else None
        try:
            result = runner.validate(bundle, image=image, artifacts_dir=artifacts_dir)
        except (BundleError, RuntimeError) as exc:
            log = f"validate failed: {exc}"
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        print(result["table"])

        report_path = REPORTS_DIR / f"{result['task_id']}-validate.json"
        Report(report_path).write(result)
        print(f"\nWrote report to {report_path}")
        if artifacts_dir is not None:
            print(f"Artifacts in {artifacts_dir}")

        log = (
            f"validate: {result['status']} "
            f"(pass2pass_ok={result['buckets']['pass2pass']['ok']} "
            f"fail2pass_ok={result['buckets']['fail2pass']['ok']})"
        )
        db.finish_run(run_id, status=STATUS_SUCCESS, results=result, log=log,
                      verdict=result["status"])

        if result["status"] != "passed":
            raise typer.Exit(code=1)


@app.command()
def run(
    bundle: Path = typer.Argument(..., help="Path to the task bundle directory."),
    solver: str = typer.Option("stub", help="Solver to use: stub | oracle | llm."),
    image: Optional[str] = typer.Option(
        None, help="Docker image to run against (defaults to the image built by `evals init`)."
    ),
    provider: Optional[str] = typer.Option(
        None,
        help="LLM provider for --solver llm: bedrock | anthropic | openai | gemini "
        "(default: TASKCLI_PROVIDER env var, then gemini).",
    ),
    model: Optional[str] = typer.Option(
        None,
        help="Model id for --solver llm (default: TASKCLI_MODEL env var, then the provider's default).",
    ),
    temperature: Optional[float] = typer.Option(
        None,
        help="Sampling temperature for --solver llm (default: 0.0, for reproducible runs).",
    ),
    thinking: Optional[str] = typer.Option(
        None,
        help="Reasoning effort for --solver llm: minimal | low | medium | high "
        "(default: medium). Higher is slower and can exhaust the output budget; "
        "measured on one task, high used 8.8x the tokens for no accuracy gain.",
    ),
    append_prompt: Optional[str] = typer.Option(
        None,
        "--append-prompt",
        help="Extra guidance appended to the solver prompt (or @path/to/file.txt to read it "
        "from a file). Added AFTER the task description and file contents - it can never "
        "replace them, so the task itself is always presented as the bundle defines it.",
    ),
    keep_artifacts: bool = typer.Option(
        False,
        "--keep-artifacts",
        help="Save the solver prompt/response, diffs, JUnit XML, test logs and the "
        "generated Dockerfile under artifacts/run-<id>/.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Print each step as it happens (container start, file reads, LLM call, test run) "
        "with elapsed times, instead of waiting silently. Useful because `go test` compiles "
        "before running and can look hung for minutes.",
    ),
    network: Optional[str] = typer.Option(
        None,
        help="Docker network for the containers (default: none). Use only for tasks that "
        "genuinely need external services - it weakens isolation.",
    ),
) -> None:
    """Run a solver in a container without hidden tests, then apply the resulting diff and grade it."""
    try:
        extra_prompt = _read_append_prompt(append_prompt)
    except OSError as exc:
        print(f"could not read --append-prompt file: {exc}")
        raise typer.Exit(code=1) from exc

    args = {
        "bundle": str(bundle), "solver": solver, "image": image,
        "provider": provider, "model": model, "keep_artifacts": keep_artifacts,
        "network": network, "temperature": temperature, "thinking": thinking,
        # The text itself, not just a flag: a run's result is meaningless without knowing
        # what extra guidance produced it, and the file it came from may have changed since.
        "append_prompt": extra_prompt,
    }
    runner = Runner(reporter=StepReporter(enabled=verbose))
    with RunDB() as db:
        run_id = db.create_run(command="run", task_id=str(bundle), args=args)
        artifacts_dir = ARTIFACTS_DIR / f"run-{run_id}" if keep_artifacts else None

        # --- Phase 1: solve (container A) -------------------------------------------
        try:
            solved = runner.solve(
                bundle, solver=solver, image=image, provider=provider, model=model,
                network=network, temperature=temperature, thinking=thinking,
                append_prompt=extra_prompt,
            )
        except SolveFailure as exc:
            # Write whatever the model produced before failing. Always - a failed run is
            # exactly when the raw response matters most, and it is otherwise unrecoverable.
            _write_solve_artifacts(artifacts_dir, exc.partial)
            log = f"run failed during solve: {exc}"
            print(log)
            if artifacts_dir is not None:
                print(f"  -> the model's raw response was saved to {artifacts_dir}")
            else:
                print("  -> re-run with --keep-artifacts to capture the model's raw response")
            db.update_run(run_id, results={"solver_metadata": exc.partial.solver_metadata})
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc
        except (BundleError, RuntimeError, ValueError) as exc:
            log = f"run failed during solve: {exc}"
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        # --- Checkpoint: the diff is the expensive, non-reproducible artifact. Persist
        # it before grading so a later crash (or a harness bug) never costs another
        # inference call - `evals resume` / `evals replay` read it back from here.
        _write_solve_artifacts(artifacts_dir, solved)
        if artifacts_dir is not None:
            # Announced HERE, where the files actually land, not only in the summary at the end.
            # Grading a compiled language runs for minutes after this point, and during all of
            # it the prompt and the model's response are already on disk - reported as missing
            # by anyone who looked, because the CLI had not mentioned them yet.
            print(f"Solver artifacts written to {artifacts_dir} (grading next).")
        db.update_run(
            run_id,
            status=STATUS_PATCH_CAPTURED,
            solver_name=solver,
            provider=(solved.solver_metadata or {}).get("provider"),
            model_id=(solved.solver_metadata or {}).get("model"),
            captured_diff=solved.diff,
            patch_applied=1 if solved.patch_applied else 0,
            patch_error=solved.patch_error,
            artifacts_dir=str(artifacts_dir) if artifacts_dir else None,
        )

        # --- Phase 2: grade (container B) -------------------------------------------
        try:
            graded = runner.grade(
                bundle, solved.diff, image=image, artifacts_dir=artifacts_dir, network=network,
            )
        except (BundleError, RuntimeError) as exc:
            log = (
                f"run failed during grading: {exc}\n"
                f"The solver's diff is saved - re-grade without re-solving via "
                f"`evals resume {run_id}`."
            )
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        task_id = _task_id_for(bundle)
        result = build_run_report(bundle, TaskBundle(bundle).task, graded_image(image, bundle),
                                  solver, solved, graded)
        _print_run_result(result, run_id, report_dir_note=artifacts_dir)

        report_path = REPORTS_DIR / f"{task_id}-run-{solver}.json"
        Report(report_path).write(result)
        print(f"\nWrote report to {report_path}  (run_id={run_id}; see `evals logs {run_id}`)")
        if artifacts_dir is not None:
            print(f"Artifacts in {artifacts_dir}")

        log = f"run: verdict={result['verdict']} solver={solver} patch_applied={result['patch_applied']}"
        db.finish_run(run_id, status=STATUS_SUCCESS, results=result, log=log,
                      verdict=result["verdict"])

        if result["verdict"] != "SOLVED":
            raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: Optional[int] = typer.Argument(
        None, help="Run id that stopped after its patch was captured. "
        "Omit to use the most recent run that has one."
    ),
    bundle: Optional[str] = typer.Option(
        None, "--bundle", help="Restrict the automatic pick to runs of this bundle."
    ),
    keep_artifacts: bool = typer.Option(False, "--keep-artifacts", help="Save grading artifacts."),
) -> None:
    """Finish a run that died after solving, re-grading its stored diff (no solver, no LLM cost)."""
    _grade_stored_diff(run_id=run_id, as_new_run=False, keep_artifacts=keep_artifacts, bundle=bundle)


@app.command()
def replay(
    run_id: Optional[int] = typer.Argument(
        None, help="Completed run id whose diff should be re-graded. "
        "Omit to use the most recent run that has one."
    ),
    bundle: Optional[str] = typer.Option(
        None, "--bundle", help="Restrict the automatic pick to runs of this bundle."
    ),
    keep_artifacts: bool = typer.Option(False, "--keep-artifacts", help="Save grading artifacts."),
) -> None:
    """Re-grade a finished run's stored diff as a NEW run - free, deterministic, no solver.

    Useful after fixing a harness bug: replay stored diffs instead of re-running inference.
    """
    _grade_stored_diff(run_id=run_id, as_new_run=True, keep_artifacts=keep_artifacts, bundle=bundle)


@app.command()
def grade(
    bundle: Path = typer.Argument(..., help="Path to the task bundle directory."),
    diff_file: Path = typer.Option(..., "--diff-file", help="Unified diff to evaluate."),
    image: Optional[str] = typer.Option(None, help="Docker image to grade against."),
    keep_artifacts: bool = typer.Option(False, "--keep-artifacts", help="Save grading artifacts."),
) -> None:
    """Grade an externally produced patch - no solver involved.

    The SWE-bench 'predictions file' model: inference happens wherever you like, and this
    only evaluates. Lets you score a patch from another agent, another harness, or a human.
    """
    args = {"bundle": str(bundle), "diff_file": str(diff_file), "image": image}
    runner = Runner()
    with RunDB() as db:
        run_id = db.create_run(command="grade", task_id=str(bundle), args=args)
        artifacts_dir = ARTIFACTS_DIR / f"run-{run_id}" if keep_artifacts else None
        try:
            diff_text = diff_file.read_text(encoding="utf-8")
            graded = runner.grade(bundle, diff_text, image=image, artifacts_dir=artifacts_dir)
        except (BundleError, RuntimeError, OSError) as exc:
            log = f"grade failed: {exc}"
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        solved = SolveResult(diff=diff_text, patch_applied=True)
        task = TaskBundle(bundle).task
        result = build_run_report(bundle, task, graded_image(image, bundle), "external", solved, graded)
        _print_run_result(result, run_id, report_dir_note=artifacts_dir)

        report_path = REPORTS_DIR / f"{task.task_id}-grade.json"
        Report(report_path).write(result)
        print(f"\nWrote report to {report_path}  (run_id={run_id})")

        db.finish_run(run_id, status=STATUS_SUCCESS, results=result, verdict=result["verdict"],
                      log=f"grade: verdict={result['verdict']} from {diff_file}")
        if result["verdict"] != "SOLVED":
            raise typer.Exit(code=1)


@app.command()
def clean(
    all_images: bool = typer.Option(False, "--all", help="Also remove evals/* images."),
) -> None:
    """Remove leftover evals containers (and optionally images)."""
    from agent_evals.docker_mgr import DockerManager

    docker = DockerManager()
    exit_code, stdout, _ = docker._run(
        ["ps", "-a", "--filter", "name=evals-", "--format", "{{.Names}}"], warn_on_failure=False
    )
    names = [n.strip() for n in stdout.splitlines() if n.strip()] if exit_code == 0 else []
    for name in names:
        docker.rm(name, force=True)
    print(f"Removed {len(names)} container(s).")

    if all_images:
        exit_code, stdout, _ = docker._run(
            ["images", "--filter", "reference=evals/*", "--format", "{{.Repository}}:{{.Tag}}"],
            warn_on_failure=False,
        )
        tags = [t.strip() for t in stdout.splitlines() if t.strip()] if exit_code == 0 else []
        for tag in tags:
            docker._run(["rmi", "-f", tag], warn_on_failure=False)
        print(f"Removed {len(tags)} image(s).")


@app.command()
def scaffold(
    instance_id: Optional[str] = typer.Option(
        None, "--instance-id",
        help="Unique row id in the dataset (e.g. 'instance_future-architect__vuls-2c84...'). "
        "Fetched directly over HTTP - no manual download needed. Mutually exclusive with --row.",
    ),
    dataset: str = typer.Option(
        DEFAULT_DATASET, help="HuggingFace dataset to fetch --instance-id from."
    ),
    split: str = typer.Option(DEFAULT_SPLIT, help="Dataset split to fetch --instance-id from."),
    row: Optional[Path] = typer.Option(
        None, "--row", help="Path to a SWE-bench (Pro) dataset row saved as JSON. "
        "Mutually exclusive with --instance-id."
    ),
    out: Path = typer.Option(..., "--out", help="Output bundle directory (created; must not be non-empty)."),
    deps_cmd: Optional[str] = typer.Option(
        None, help="Override deps_cmd (default: the dataset image needs none, else a per-language default)."
    ),
    test_cmd: Optional[str] = typer.Option(
        None, help="Override test_cmd ({path} = bucket dir, {report} = JUnit XML path)."
    ),
    use_dataset_image: bool = typer.Option(
        True, "--use-dataset-image/--build-image",
        help="Use SWE-bench Pro's prebuilt per-instance image (deps already installed) "
        "instead of building from a language base image.",
    ),
) -> None:
    """Generate a task bundle from a SWE-bench dataset row (repo, commit, patches, test buckets).

    Give either --instance-id (fetched live from the dataset, nothing to download by hand)
    or --row (a JSON row you already have saved). Exactly one is required.
    """
    import json as _json

    if bool(instance_id) == bool(row):
        print("scaffold requires exactly one of --instance-id or --row.")
        raise typer.Exit(code=1)

    args = {
        "instance_id": instance_id, "dataset": dataset, "split": split,
        "row": str(row) if row else None, "out": str(out), "deps_cmd": deps_cmd,
        "test_cmd": test_cmd, "use_dataset_image": use_dataset_image,
    }
    with RunDB() as db:
        run_id = db.create_run(command="scaffold", task_id=str(out), args=args)
        try:
            from agent_evals.scaffold import fetch_dataset_row, scaffold_bundle

            if instance_id:
                row_data = fetch_dataset_row(instance_id, dataset=dataset, split=split)
            else:
                row_data = _json.loads(row.read_text(encoding="utf-8"))
                # Accept either a bare row dict or the HF datasets-server envelope.
                if "rows" in row_data:
                    row_data = row_data["rows"][0]["row"]

            result = scaffold_bundle(
                row_data, out, deps_cmd=deps_cmd, test_cmd=test_cmd,
                use_dataset_image=use_dataset_image,
            )
        except (BundleError, OSError, KeyError, ValueError) as exc:
            log = f"scaffold failed: {exc}"
            print(log)
            db.finish_run(run_id, status=STATUS_ERROR, log=log)
            raise typer.Exit(code=1) from exc

        log = (
            f"scaffold: wrote bundle {result['task_id']} to {result['out_dir']} "
            f"(repo={result['repo']} commit={result['commit'][:12]} "
            f"f2p={result['fail2pass_tests']} p2p={result['pass2pass_tests']} "
            f"hidden={', '.join(result['hidden_paths'])})"
        )
        print(log)
        print(f"\nNext: evals init {result['out_dir']} && evals validate {result['out_dir']}")
        db.finish_run(run_id, status=STATUS_SUCCESS, results=result, log=log)


@app.command()
def providers(
    provider: Optional[str] = typer.Option(
        None, help="Check just this provider (default: check all registered providers)."
    ),
    model: Optional[str] = typer.Option(None, help="Model id to test with."),
) -> None:
    """Check LLM provider credentials with one tiny call, before running a real task."""
    from agent_evals.solvers import PROVIDERS, get_provider

    names = [provider] if provider else sorted(PROVIDERS)
    any_ok = False
    for name in names:
        try:
            p = get_provider(name, model=model)
        except ValueError as exc:
            print(f"{name:<10} CONFIG ERROR  {exc}")
            continue
        try:
            reply = p.complete("Reply with exactly: OK")
            any_ok = True
            print(f"{name:<10} OK            model={p.model} reply={reply.strip()[:40]!r}")
        except RuntimeError as exc:
            first_line = str(exc).splitlines()[0]
            print(f"{name:<10} UNAVAILABLE   model={p.model}\n{' ' * 12}{first_line}")

    if not any_ok:
        print("\nNo provider is usable yet. See the README's 'Configuring an LLM provider' section.")
        raise typer.Exit(code=1)


@app.command()
def logs(
    run_id: Optional[int] = typer.Argument(
        None, help="Run ID to show logs for. Omit to show the most recent run that has a log."
    ),
) -> None:
    """Show the log output recorded for a given run."""
    with RunDB() as db:
        if run_id is None:
            run_id = db.latest_run_id(with_log=True)
            if run_id is None:
                print("No run has recorded a log yet. Try `evals history`.")
                raise typer.Exit(code=1)
            print(f"# run {run_id} (most recent with a log)")
        record = db.get_run(run_id)
    if record is None:
        print(f"No run found with run_id={run_id}")
        raise typer.Exit(code=1)
    print(record["log"] or "(no log recorded)")


@app.command()
def history(limit: int = typer.Option(20, help="Number of recent runs to show.")) -> None:
    """List recent runs, most recent first."""
    with RunDB() as db:
        records = db.list_runs(limit=limit)
    if not records:
        print("No runs recorded yet.")
        return
    for r in records:
        outcome = r.get("verdict") or r.get("status")
        resumable = " (resumable)" if r.get("status") == STATUS_PATCH_CAPTURED else ""
        source = f" <-run{r['source_run_id']}" if r.get("source_run_id") else ""
        print(
            f"[{r['run_id']}] {r.get('created_at') or r.get('ts')} {r['command']:<9} "
            f"task={r['task_id']} {outcome}{resumable}{source}"
        )


if __name__ == "__main__":
    app()
