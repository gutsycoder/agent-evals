# Runs the Task Bundle CLI itself on Linux, driving the host's Docker daemon over a
# mounted socket ("Docker outside of Docker"). The containers the CLI spawns are siblings
# on the same daemon, so this exercises the real code paths - subprocess encoding, POSIX
# path handling, docker cp/exec, file ownership - on Linux rather than Windows.
#
# Build:  docker build -f tests/linux/Dockerfile -t evals-linux-test .
# Run:    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock evals-linux-test \
#             sh tests/linux/run_linux_suite.sh
FROM python:3.11-slim

# Only the docker *client* is needed - the daemon comes from the mounted socket. The
# distro docker.io package doesn't ship the client under --no-install-recommends, so pull
# the official static binary instead (small, and version-pinned).
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e .

CMD ["/bin/bash"]
