"""Tiny in-process Prometheus-style metrics.

Counters and a histogram stored in memory; emitted at /metrics in
text/plain. Good enough for a single-process app + a Prometheus scraper.
For multi-worker deployments use prometheus_client with a multiprocess
directory.
"""
from __future__ import annotations

import threading
from collections import defaultdict


_lock = threading.Lock()
_counters: dict[tuple[str, tuple], int] = defaultdict(int)
_histograms: dict[tuple[str, tuple], list] = defaultdict(list)

_REQUEST_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


def inc(name: str, labels: dict[str, str] | None = None, by: int = 1) -> None:
    key = (name, tuple(sorted((labels or {}).items())))
    with _lock:
        _counters[key] += by


def observe(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    key = (name, tuple(sorted((labels or {}).items())))
    with _lock:
        _histograms[key].append(value)


def reset() -> None:
    """Tests use this to start clean."""
    with _lock:
        _counters.clear()
        _histograms.clear()


def render() -> str:
    """Render the metrics in Prometheus text exposition format."""
    out: list[str] = []
    seen_help: set[str] = set()

    def _help(name: str, type_: str, description: str):
        if name in seen_help:
            return
        seen_help.add(name)
        out.append(f"# HELP {name} {description}")
        out.append(f"# TYPE {name} {type_}")

    def _labels(items: tuple) -> str:
        if not items:
            return ""
        return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"

    with _lock:
        for (name, labels), count in sorted(_counters.items()):
            _help(name, "counter", f"{name} count")
            out.append(f"{name}{_labels(labels)} {count}")
        for (name, labels), values in sorted(_histograms.items()):
            _help(name, "histogram", f"{name} observations")
            count = len(values)
            total = sum(values)
            for b in _REQUEST_BUCKETS:
                bucket_count = sum(1 for v in values if v <= b)
                bucket_labels = labels + (("le", str(b)),)
                out.append(f"{name}_bucket{_labels(bucket_labels)} {bucket_count}")
            inf_labels = labels + (("le", "+Inf"),)
            out.append(f"{name}_bucket{_labels(inf_labels)} {count}")
            out.append(f"{name}_sum{_labels(labels)} {total}")
            out.append(f"{name}_count{_labels(labels)} {count}")
    return "\n".join(out) + "\n"
