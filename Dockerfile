# Forge control-plane API + engine image.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# psycopg2-binary needs no build deps; keep the image lean.
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e . \
    && useradd --create-home --uid 10001 forge \
    && chown -R forge /app
USER forge

ENV FORGE_HOME=/home/forge/.forge \
    FORGE_CONTROL_HOST=0.0.0.0 \
    FORGE_CONTROL_PORT=8787

EXPOSE 8787

# Default: serve the control-plane API. Override the command to run the loop
# (`forge run ...`) in a sibling container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3).status==200 else 1)" || exit 1

CMD ["forge", "serve"]
