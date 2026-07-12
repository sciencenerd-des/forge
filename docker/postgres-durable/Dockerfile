# Forge's opt-in PostgreSQL substrate. This is deliberately separate from the
# default pgvector service because several requested workers require
# shared_preload_libraries and pg_durable requires PostgreSQL 17.
FROM golang:1.25-bookworm AS timetable-builder
ARG PG_TIMETABLE_VERSION=v6.3.0
RUN git clone --depth 1 --branch "${PG_TIMETABLE_VERSION}" \
      https://github.com/cybertec-postgresql/pg_timetable.git /tmp/pg_timetable \
    && cd /tmp/pg_timetable \
    && CGO_ENABLED=0 go build -buildvcs=false -o /pg_timetable .

FROM pgvector/pgvector:pg17

ARG PGMQ_VERSION=v1.4.4
ARG PARTMAN_VERSION=5.1.0
ARG PGNET_VERSION=v0.9.3
ARG PGTASK_VERSION=v1.0.0
ARG PGLATER_VERSION=v0.4.0
ARG PGDURABLE_VERSION=v0.2.3
ARG PGAI_VERSION=extension-0.11.2

ENV DEBIAN_FRONTEND=noninteractive \
    PG_MAJOR=17 \
    PGAI_VERSION=${PGAI_VERSION}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git pkg-config \
      libcurl4-openssl-dev libssl-dev libkrb5-dev liblz4-dev libzstd-dev pcregrep \
      postgresql-server-dev-17 postgresql-17-cron postgresql-plpython3-17 \
      python3 python3-pip \
      clang llvm libclang-dev \
    && rm -rf /var/lib/apt/lists/*

COPY extension-manifest.env /usr/local/share/forge/extension-manifest.env
COPY install-extensions.sh /usr/local/sbin/install-forge-extensions
RUN chmod 0755 /usr/local/sbin/install-forge-extensions \
    && /usr/local/sbin/install-forge-extensions

COPY --from=timetable-builder /pg_timetable /usr/local/bin/pg_timetable

RUN mkdir -p /usr/share/postgresql/postgresql.conf.sample.d \
    && printf '%s\n' \
      "max_worker_processes = 64" \
      "shared_preload_libraries = 'pg_cron,pg_partman_bgw,pg_net,pg_task,pg_later,pg_durable'" \
      "cron.database_name = 'forge'" \
      "pg_partman_bgw.dbname = 'forge'" \
      "pg_partman_bgw.role = 'forge'" \
      "pg_net.database_name = 'forge'" \
      "pg_later.database_name = 'forge'" \
      "pg_task.json = '[{\"data\":\"forge\",\"user\":\"forge\"}]'" \
      "pg_durable.worker_role = 'forge'" \
      "pg_durable.database = 'forge'" \
      > /usr/share/postgresql/postgresql.conf.sample.d/forge-durable.conf

# The migration creates extensions in the application database after the
# tables exist. It is intentionally not an initdb hook: extension SQL such as
# the vectorscale index depends on Forge's tables being present first.
