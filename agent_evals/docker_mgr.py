"""Thin wrappers over the docker CLI via subprocess."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CommandResult = tuple[int, str, str]


class DockerManager:
    def __init__(self, docker_bin: str = "docker") -> None:
        self.docker_bin = docker_bin

    def _run(self, args: list[str], *, warn_on_failure: bool = True) -> CommandResult:
        cmd = [self.docker_bin, *args]
        logger.info("docker command: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"'{self.docker_bin}' was not found on PATH. Install Docker and ensure it is running."
            ) from exc
        if proc.returncode != 0 and warn_on_failure:
            logger.warning(
                "docker command failed (exit %d): %s\nstderr: %s",
                proc.returncode, " ".join(cmd), proc.stderr.strip(),
            )
        return proc.returncode, proc.stdout, proc.stderr

    def image_exists(self, tag: str) -> bool:
        # A nonzero exit here just means "not built yet" - not a real failure, so don't warn.
        exit_code, _, _ = self._run(["image", "inspect", tag], warn_on_failure=False)
        return exit_code == 0

    def _run_streaming(self, args: list[str]) -> CommandResult:
        """Run a docker command, echoing its output live AND capturing it.

        `subprocess.run(capture_output=True)` swallows everything until the process exits,
        which is fine for a `docker exec` that takes a second and actively harmful for a
        build. A first-time `evals init` on a SWE-bench Pro bundle pulls several GB, and with
        the output captured that is 30-45 minutes of a completely silent terminal - which is
        indistinguishable from a hang, and was reported as one.

        Streamed line by line rather than inherited wholesale, so the text is still available
        for the ledger and the report. Because stdout is a pipe rather than a TTY, BuildKit
        automatically emits its plain line-oriented progress instead of the ANSI display that
        rewrites lines in place - which is what makes tee-ing readable. No `--progress` flag
        is passed, deliberately: it is BuildKit-only and would break anyone running with
        DOCKER_BUILDKIT=0.
        """
        cmd = [self.docker_bin, *args]
        logger.info("docker command (streaming): %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # one interleaved stream, in the order it happened
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,                  # line buffered, so progress appears as it arrives
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"'{self.docker_bin}' was not found on PATH. Install Docker and ensure it is running."
            ) from exc

        captured: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            captured.append(line)
            print(f"  {line}", flush=True)
        exit_code = proc.wait()
        output = "\n".join(captured)
        if exit_code != 0:
            logger.warning("docker command failed (exit %d): %s", exit_code, " ".join(cmd))
        return exit_code, output, output

    def build(self, context: Path, tag: str, dockerfile: Optional[Path] = None,
              stream: bool = True) -> CommandResult:
        args = ["build", "-t", tag]
        if dockerfile is not None:
            args += ["-f", str(dockerfile)]
        args.append(str(context))
        # Streamed by default: a build is the one docker call here that can run for tens of
        # minutes, and it is always worth watching.
        return self._run_streaming(args) if stream else self._run(args)

    def run_detached(
        self,
        image: str,
        name: str,
        extra_flags: Optional[list[str]] = None,
        *,
        network_none: bool = True,
        network: Optional[str] = None,
        memory: Optional[str] = "2g",
        user: Optional[str] = None,
        command: Optional[list[str]] = None,
    ) -> CommandResult:
        args = ["run", "-d", "--name", name]
        if network:
            # Explicit opt-in, e.g. an integration task that needs a live service.
            args += ["--network", network]
        elif network_none:
            args += ["--network", "none"]
        if memory:
            args += ["--memory", memory]
        if user:
            args += ["--user", user]
        if command:
            # Override whatever ENTRYPOINT the base image bakes in. Without this,
            # `command` is appended as ARGS to that entrypoint rather than executed
            # directly - some third-party images (e.g. SWE-bench Pro's dataset images)
            # set ENTRYPOINT to a shell, so `sleep infinity` silently became
            # `/bin/bash sleep infinity`: bash treats "sleep" as a script filename to
            # read, finds the ELF binary, and fails with "cannot execute binary file".
            # The container then never starts, and every later `docker exec` against it
            # fails with "container is not running" - which is what actually happened.
            args += ["--entrypoint", command[0]]
        if extra_flags:
            args += list(extra_flags)
        args.append(image)
        if command:
            args += list(command[1:])
        return self._run(args)

    def exec(
        self,
        container: str,
        cmd: list[str],
        *,
        user: Optional[str] = None,
        workdir: Optional[str] = None,
        warn_on_failure: bool = True,
    ) -> CommandResult:
        args = ["exec"]
        if user:
            args += ["--user", user]
        if workdir:
            args += ["--workdir", workdir]
        args.append(container)
        args += list(cmd)
        return self._run(args, warn_on_failure=warn_on_failure)

    def exec_capture_bytes(self, container: str, cmd: list[str]) -> tuple[int, bytes, bytes]:
        """Like exec(), but returns raw stdout/stderr bytes instead of decoded text.

        Needed for piping binary data (e.g. a tar stream) out of a container -
        text=True would corrupt non-UTF-8 bytes.
        """
        full_cmd = [self.docker_bin, "exec", container, *cmd]
        logger.info("docker command: %s", " ".join(full_cmd))
        try:
            proc = subprocess.run(full_cmd, capture_output=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"'{self.docker_bin}' was not found on PATH. Install Docker and ensure it is running."
            ) from exc
        if proc.returncode != 0:
            logger.warning(
                "docker command failed (exit %d): %s\nstderr: %s",
                proc.returncode, " ".join(full_cmd), proc.stderr.decode("utf-8", "replace").strip(),
            )
        return proc.returncode, proc.stdout, proc.stderr

    def exec_stdin(self, container: str, cmd: list[str], data: bytes) -> CommandResult:
        """Run a command in the container with `data` piped to its stdin.

        Used to move diffs in without a host temp file + `docker cp` - fewer moving parts
        and no text-mode write to mangle line endings on Windows. SWE-bench's harness
        likewise feeds patches over stdin (`git apply -v -`).
        """
        full_cmd = [self.docker_bin, "exec", "-i", container, *cmd]
        logger.info("docker command: %s (stdin: %d bytes)", " ".join(full_cmd), len(data))
        try:
            proc = subprocess.run(full_cmd, input=data, capture_output=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"'{self.docker_bin}' was not found on PATH. Install Docker and ensure it is running."
            ) from exc
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            logger.warning(
                "docker command failed (exit %d): %s\nstderr: %s",
                proc.returncode, " ".join(full_cmd), stderr.strip(),
            )
        return proc.returncode, stdout, stderr

    def cp_to(self, container: str, src: Path, dst: str) -> CommandResult:
        return self._run(["cp", str(src), f"{container}:{dst}"])

    def cp_from(self, container: str, src: str, dst: Path) -> CommandResult:
        return self._run(["cp", f"{container}:{src}", str(dst)])

    def pull(self, image: str) -> CommandResult:
        """Pull an image into the local store.

        Needed because `docker inspect` reads only the local store, while BuildKit - the
        default builder - pulls base images into its own cache. An image can therefore build
        fine and still be invisible to inspect, which made a workdir lookup silently return
        nothing. Pulls can be slow, so callers should only do this when inspect actually failed.

        Streamed for the same reason as build: SWE-bench Pro's per-instance images are several
        GB, and a silent terminal for half an hour reads as a hang.
        """
        return self._run_streaming(["pull", image])

    def image_workdir(self, image: str) -> str:
        """The WORKDIR an image declares, or "" if it declares none.

        Used to work *inside* the directory a prebuilt image already prepared, rather than
        cloning a second copy of the repo somewhere it knows nothing about. SWE-bench Pro's
        images ship the repo at their WORKDIR with dependencies installed and pointing there;
        a parallel checkout elsewhere gets patched but never imported, so a correct patch
        grades as a failure. Read from the image rather than hardcoded, so this needs no
        per-language or per-publisher table.
        """
        exit_code, stdout, _ = self._run(
            ["inspect", "--format", "{{.Config.WorkingDir}}", image], warn_on_failure=False
        )
        return stdout.strip() if exit_code == 0 else ""

    def rm(self, container: str, *, force: bool = True) -> CommandResult:
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(container)
        return self._run(args)
