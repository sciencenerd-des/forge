"""Regression coverage for telemetry's no-op failure boundary."""
from contextlib import contextmanager

import pytest

import forge_runtime.telemetry as telemetry


class _Span:
    def set_attribute(self, key, value):
        pass


class _Tracer:
    @contextmanager
    def start_as_current_span(self, name):
        yield _Span()


def test_span_preserves_the_observed_operation_exception(monkeypatch):
    monkeypatch.setattr(telemetry, "_init", lambda: None)
    monkeypatch.setattr(telemetry, "_tracer", _Tracer())

    with pytest.raises(ValueError, match="original failure"):
        with telemetry.span("node"):
            raise ValueError("original failure")
