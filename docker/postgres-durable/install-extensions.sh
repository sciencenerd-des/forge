#!/usr/bin/env bash
set -Eeuo pipefail

# Build-time installer for the durable image. pg_timetable and the pgai
# Vectorizer are services, not CREATE EXTENSION names; compose runs them as
# separate workers. pg_task is a preload-only background worker: upstream
# ships a shared library and creates its task table at startup, but does not
# ship a PostgreSQL extension control/SQL package. The checks below reflect
# those real packaging contracts instead of manufacturing a control file.

: "${PG_MAJOR:=17}"
: "${PGMQ_VERSION:=v1.4.4}"
: "${PARTMAN_VERSION:=5.1.0}"
: "${PGNET_VERSION:=v0.9.3}"
: "${PGTASK_VERSION:=v1.0.0}"
: "${PGLATER_VERSION:=v0.4.0}"
: "${PGDURABLE_VERSION:=v0.2.3}"
: "${PGAI_VERSION:=extension-0.11.2}"
: "${PGVECTORSCALE_VERSION:=0.5.1}"
: "${PG_RUNTIME_ROLE:=forge}"

PGMQ_SQL_VERSION="${PGMQ_VERSION#v}"
PGNET_SQL_VERSION="${PGNET_VERSION#v}"

export PATH="/root/.cargo/bin:${PATH}"
export PG_CONFIG="/usr/lib/postgresql/${PG_MAJOR}/bin/pg_config"

apt-get update
apt-get install -y --no-install-recommends unzip rustc cargo

git clone --depth 1 --branch "${PGMQ_VERSION}" https://github.com/pgmq/pgmq.git /tmp/pgmq
make -C /tmp/pgmq/pgmq-extension "sql/pgmq--${PGMQ_SQL_VERSION}.sql"
make -C /tmp/pgmq/pgmq-extension install

curl -fsSL "https://github.com/pgpartman/pg_partman/archive/refs/tags/v${PARTMAN_VERSION}.tar.gz" \
  | tar -xz -C /tmp
make -C "/tmp/pg_partman-${PARTMAN_VERSION}" install

git clone --depth 1 --branch "${PGNET_VERSION}" https://github.com/supabase/pg_net.git /tmp/pg_net
make -C /tmp/pg_net "sql/pg_net--${PGNET_SQL_VERSION}.sql" pg_net.control
# pg_net's upstream SQL assumes the stock image's `postgres` role. Forge's
# image deliberately bootstraps the least-privilege application role instead;
# rewrite only those ownership grants before installing the generated script.
sed -i -E "s/(grant all on (schema|all tables in schema) net to )postgres;/\\1${PG_RUNTIME_ROLE};/" \
  "/tmp/pg_net/sql/pg_net--${PGNET_SQL_VERSION}.sql"
make -C /tmp/pg_net install

git clone --depth 1 --branch "${PGTASK_VERSION}" https://github.com/RekGRpth/pg_task.git /tmp/pg_task
make -C /tmp/pg_task install

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y --profile minimal --default-toolchain stable
cargo install cargo-pgrx --version 0.16.1 --locked
cargo pgrx init --pg17="${PG_CONFIG}"
git clone --depth 1 --branch "${PGLATER_VERSION}" https://github.com/tembo-io/pg_later.git /tmp/pg_later
cd /tmp/pg_later
cargo pgrx install --manifest-path Cargo.toml --release \
  --no-default-features --features pg17 --pg-config="${PG_CONFIG}"
git clone --depth 1 --branch "${PGDURABLE_VERSION}" https://github.com/microsoft/pg_durable.git /tmp/pg_durable
cd /tmp/pg_durable
cargo pgrx install --manifest-path Cargo.toml --release \
  --no-default-features --features pg17 --pg-config="${PG_CONFIG}"

arch="$(dpkg --print-architecture)"
case "${arch}" in
  amd64|arm64) ;;
  *) echo "unsupported architecture for pgvectorscale release: ${arch}" >&2; exit 1 ;;
esac
curl -fsSL -o /tmp/vectorscale.zip \
  "https://github.com/timescale/pgvectorscale/releases/download/${PGVECTORSCALE_VERSION}/pgvectorscale-${PGVECTORSCALE_VERSION}-pg${PG_MAJOR}-${arch}.zip"
unzip -q /tmp/vectorscale.zip -d /tmp/vectorscale
dpkg -i /tmp/vectorscale/pgvectorscale-postgresql-${PG_MAJOR}_*.deb

# pgai's `ai` extension supplies SQL model calls and Vectorizer definitions.
# Its worker is a separate service and is not a PostgreSQL shared library.
git clone --depth 1 --branch "${PGAI_VERSION}" https://github.com/timescale/pgai.git /tmp/pgai
pip3 install --no-cache-dir --break-system-packages uv==0.6.3
cd /tmp/pgai/projects/extension
uv run python build.py build
uv run python build.py install-py
uv run python build.py install-sql all

for control in vector vectorscale ai pgmq pg_cron pg_partman pg_net pg_later pg_durable; do
  test -f "$(pg_config --sharedir)/extension/${control}.control" \
    || { echo "missing PostgreSQL extension control file: ${control}" >&2; exit 1; }
done
test -f "$(pg_config --pkglibdir)/pg_task.so" \
  || { echo "missing pg_task preload library" >&2; exit 1; }

rm -rf /tmp/pgmq /tmp/pg_partman-* /tmp/pg_net /tmp/pg_task /tmp/pg_later \
  /tmp/pg_durable /tmp/pgai /tmp/vectorscale /tmp/vectorscale.zip
apt-get clean
rm -rf /var/lib/apt/lists/*
