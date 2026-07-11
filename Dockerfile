# Forge control-plane API + engine image.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# psycopg2-binary needs no build deps; keep the image lean.
WORKDIR /app

# Docker CLI only (no daemon/systemd) — forge_runtime/sandbox.py shells out
# to `docker` to create per-project sandbox containers on the HOST's daemon
# via the socket docker-compose.yml mounts in (Docker-outside-of-Docker).
# Static official binary keeps this a single small download instead of
# pulling the full docker.io package tree.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://download.docker.com/linux/static/stable/$(dpkg --print-architecture | sed 's/amd64/x86_64/;s/arm64/aarch64/')/docker-27.3.1.tgz \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e . \
    && useradd --create-home --uid 10001 forge \
    && chown -R forge /app
USER forge
# NOTE: mounting /var/run/docker.sock (see docker-compose.yml) is itself
# root-equivalent access to the host's Docker daemon regardless of this
# container's own UID — dropping to a non-root user here does not reduce
# that. It's kept anyway for defense-in-depth against everything ELSE this
# container does (serving the API, running local code). If the mounted
# socket's host-side group doesn't grant `forge` (uid 10001) permission,
# forge_runtime/sandbox.py fails closed instead of silently changing to host
# execution. Operators can explicitly opt into host mode only when that risk
# is acceptable.

ENV FORGE_HOME=/home/forge/.forge \
    FORGE_CONTROL_HOST=0.0.0.0 \
    FORGE_CONTROL_PORT=8787

EXPOSE 8787

# Default: serve the control-plane API. Override the command to run the loop
# (`forge run ...`) in a sibling container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3).status==200 else 1)" || exit 1

CMD ["forge", "serve"]
