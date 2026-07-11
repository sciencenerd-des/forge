"""OpenTelemetry instrumentation for the PGE loop.

Gives the harness itself observability: one span per PGE node
(planner/auditor/executor/evaluator) and one child span per tool call, plus
counters for tool invocations and gate blocks (mutation/vacuous/overfit/
scope). Exported via OTLP to a collector so traces can be viewed in
Jaeger/Grafana/etc. — see docker-compose.yml's ``otel-collector`` service.

Degrades to a no-op tracer (never raises, never blocks the loop) whenever
the collector is unreachable or telemetry is disabled — the reward channel
must never depend on an optional observability sidecar being up. This
mirrors the existing graceful-degradation pattern in
forge_runtime/verifier_hierarchy.py (``docker_available()``).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_SERVICE_NAME = "forge-hermes"


def telemetry_enabled() -> bool:
    return os.getenv("FORGE_OTEL_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


_tracer = None
_tool_call_counter = None
_gate_block_counter = None
_turn_duration_histogram = None
_initialized = False


def _collector_reachable(endpoint: str, timeout: float = 0.3) -> bool:
    """Fast TCP probe so an absent collector degrades to silent no-op instead
    of a BatchSpanProcessor retrying (and logging warnings) forever — the
    same "optional infra, fail quiet" contract as docker_available() in
    verifier_hierarchy.py."""
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
        host = parsed.hostname or "localhost"
        port = parsed.port or 4317
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _init() -> None:
    global _tracer, _tool_call_counter, _gate_block_counter, _turn_duration_histogram, _initialized
    if _initialized:
        return
    _initialized = True
    if not telemetry_enabled():
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    if not _collector_reachable(endpoint):
        print(f"[telemetry] no OTel collector reachable at {endpoint} — running without tracing "
              "(start `docker compose up -d otel-collector` to enable).")
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": _SERVICE_NAME})

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(_SERVICE_NAME)

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=15_000
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter(_SERVICE_NAME)
        _tool_call_counter = meter.create_counter(
            "forge.tool_calls", description="Executor tool invocations, by tool name and outcome")
        _gate_block_counter = meter.create_counter(
            "forge.gate_blocks", description="Completion gate blocks, by gate name")
        _turn_duration_histogram = meter.create_histogram(
            "forge.node_duration_ms", description="PGE node duration in milliseconds", unit="ms")
    except Exception as e:  # pragma: no cover - exercised via disabled-mode tests
        print(f"[telemetry] OpenTelemetry unavailable ({str(e)[:120]}) — running without tracing.")
        _tracer = None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Start a span named ``forge.<name>``; no-ops entirely if telemetry is
    unavailable or disabled. Never raises — a broken exporter must never
    break the autonomy loop."""
    _init()
    if _tracer is None:
        yield
        return
    try:
        span_context = _tracer.start_as_current_span(f"forge.{name}")
        active_span = span_context.__enter__()
    except Exception:
        # Telemetry setup is optional. Do not make an unavailable exporter a
        # dependency of the node being observed.
        yield
        return

    try:
        for key, value in attributes.items():
            try:
                active_span.set_attribute(key, value)
            except Exception:
                pass
        yield
    except BaseException as error:
        # Preserve the observed operation's original exception. In
        # particular, never re-enter the generator here: that turns an
        # application error into contextmanager's "generator didn't stop".
        try:
            span_context.__exit__(type(error), error, error.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            span_context.__exit__(None, None, None)
        except Exception:
            pass


def record_tool_call(tool_name: str, ok: bool) -> None:
    _init()
    if _tool_call_counter is None:
        return
    try:
        _tool_call_counter.add(1, {"tool": tool_name, "outcome": "ok" if ok else "error"})
    except Exception:
        pass


def record_gate_block(gate_name: str) -> None:
    _init()
    if _gate_block_counter is None:
        return
    try:
        _gate_block_counter.add(1, {"gate": gate_name})
    except Exception:
        pass


def record_node_duration(node_name: str, duration_ms: float) -> None:
    _init()
    if _turn_duration_histogram is None:
        return
    try:
        _turn_duration_histogram.record(duration_ms, {"node": node_name})
    except Exception:
        pass
