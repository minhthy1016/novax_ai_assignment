"""Prometheus metrics.

Labels are deliberately low-cardinality: route templates (not raw paths), provider and
model names, tool names, and outcome enums. User IDs and document IDs never become labels.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

HTTP_REQUESTS = Counter(
    "opsassist_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_REQUEST_LATENCY = Histogram(
    "opsassist_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)

MODEL_CALLS = Counter(
    "opsassist_model_calls_total", "Model provider calls", ["provider", "model", "outcome"]
)
MODEL_LATENCY = Histogram(
    "opsassist_model_call_duration_seconds",
    "Model provider call latency",
    ["provider", "model"],
    buckets=_LATENCY_BUCKETS,
)
MODEL_TOKENS = Counter(
    "opsassist_model_tokens_total", "Model tokens", ["provider", "model", "direction"]
)

TOOL_CALLS = Counter(
    "opsassist_tool_calls_total", "Tool invocations", ["tool", "decision", "outcome"]
)

RETRIEVAL_LATENCY = Histogram(
    "opsassist_retrieval_duration_seconds",
    "Retrieval latency (embed + search + filter)",
    buckets=_LATENCY_BUCKETS,
)
