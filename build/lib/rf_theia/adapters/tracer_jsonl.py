"""Trace feed adapter for the T Sig keyword family.

Module name preserved as ``tracer_jsonl`` for symmetry with the planning
doc, but the wire format is line-oriented text, not JSON — Tracer.hh
(platform/runtime/include/Tracer.hh) writes one record per line to
stderr in the form::

    TRC v1 <event> <node> msg=<type_name> corr=<u32> ts=<N>ms hex=<...>

This adapter tails a file (typically the supervisor's captured stderr,
or a per-node log file) and:

  - parses each line into a :class:`TraceRecord`
  - exposes ``expect()`` / ``expect_order()`` / ``expect_latency()``
    polling-style assertions used by T Sig keywords
  - keeps a buffered history (``filter()``) for offline pandas-style
    queries via the assessment module

A TCP feed (live streaming from the trace service) is on the roadmap
but not wired yet — file-tail covers the smoke scenarios.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

# TRC v1 <event> <node> msg=<type> corr=<id> ts=<N>ms hex=<hexdata>
_TRC_RE = re.compile(
    r"^TRC v1 "
    r"(?P<event>\S+) "
    r"(?P<node>\S+) "
    r"msg=(?P<msg>\S+) "
    r"corr=(?P<corr>\d+) "
    r"ts=(?P<ts_ms>\d+)ms "
    r"hex=(?P<hex>\S*)$"
)


@dataclass
class TraceRecord:
    """One trace event. Matches the wire format defined in Tracer.hh."""

    event: str
    node: str
    msg_type: str
    corr_id: int
    ts_ms: int
    payload_hex: str = ""

    @classmethod
    def parse(cls, line: str) -> "TraceRecord | None":
        m = _TRC_RE.match(line.strip())
        if m is None:
            return None
        return cls(
            event=m["event"],
            node=m["node"],
            msg_type=m["msg"],
            corr_id=int(m["corr"]),
            ts_ms=int(m["ts_ms"]),
            payload_hex=m["hex"],
        )


class TraceFeed:
    """Tail-based trace consumer. Background thread reads new lines as
    they're appended; the main thread runs assertion polling against the
    accumulated history.

    Supports two source forms:

      - ``file:///path/to/log``  — tail a local file
      - ``/path/to/log``          — same, shorthand
      - ``tcp://host:port``       — placeholder; raises NotImplementedError

    Records are kept in memory; ``close()`` stops the tail thread.
    """

    def __init__(self, source: str, poll_interval: float = 0.05) -> None:
        self.source = source
        self.poll_interval = poll_interval
        self._records: list[TraceRecord] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fh = None

    # ----- lifecycle --------------------------------------------------

    def open(self) -> None:
        path = self._resolve_file_source()
        # If the file doesn't exist yet, create it — supervisor stderr
        # may not have started writing. tail-style semantics. mkdir the
        # parent so a "well-known but not yet present" path
        # (/tmp/theia/sm.log) works on first run.
        if not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            open(path, "a").close()
        self._fh = open(path, "r")
        # Seek to end so we only see records emitted from now on.
        self._fh.seek(0, os.SEEK_END)
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _resolve_file_source(self) -> str:
        s = self.source
        if s.startswith("tcp://"):
            raise NotImplementedError(
                "TCP trace feed not implemented yet — pass a file path"
            )
        if s.startswith("file://"):
            return s[len("file://"):]
        return s

    def _reader(self) -> None:
        assert self._fh is not None
        buf = ""
        while not self._stop.is_set():
            chunk = self._fh.read()
            if not chunk:
                time.sleep(self.poll_interval)
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                rec = TraceRecord.parse(line)
                if rec is not None:
                    with self._lock:
                        self._records.append(rec)

    # ----- read accessors --------------------------------------------

    def snapshot(self) -> list[TraceRecord]:
        with self._lock:
            return list(self._records)

    def filter(self, **where: object) -> list[TraceRecord]:
        """Return records matching all ``field == value`` constraints.

        Example::

            feed.filter(event="recv", node="sm_daemon")
        """
        out: list[TraceRecord] = []
        for r in self.snapshot():
            if all(getattr(r, k, None) == v for k, v in where.items()):
                out.append(r)
        return out

    # ----- assertions -------------------------------------------------

    def expect(self, event: str, node: str = "", timeout: float = 2.0,
               msg_type: str = "") -> TraceRecord:
        """Poll until a record matching the criteria appears, or raise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in self.snapshot():
                if r.event != event:
                    continue
                if node and r.node != node:
                    continue
                if msg_type and r.msg_type != msg_type:
                    continue
                return r
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"trace: no record event={event!r} node={node!r} "
            f"msg_type={msg_type!r} within {timeout}s"
        )

    def expect_order(
        self,
        events: Iterable[str],
        same_correlation: bool = True,
        timeout: float = 5.0,
    ) -> list[TraceRecord]:
        """Assert that the given event names appear in order.

        When ``same_correlation=True`` (default), all matched records
        must share a single ``corr_id``. Use this for assertions like
        "this RPC fired Send → Recv → Dispatch → DispatchDone".

        Returns the matched records in order.
        """
        wanted = list(events)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            recs = self.snapshot()
            matched = _match_order(recs, wanted, same_correlation)
            if matched is not None:
                return matched
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"trace: did not see ordered sequence {wanted!r} "
            f"(same_correlation={same_correlation}) within {timeout}s"
        )

    def expect_latency(
        self, from_event: str, to_event: str, lt: float,
        timeout: float = 5.0,
    ) -> float:
        """Assert that some ``from_event → to_event`` pair (same
        correlation id) completes within ``lt`` seconds. Returns the
        observed latency in seconds."""
        pair = self.expect_order([from_event, to_event],
                                 same_correlation=True, timeout=timeout)
        latency_s = (pair[1].ts_ms - pair[0].ts_ms) / 1000.0
        if latency_s > lt:
            raise AssertionError(
                f"trace: latency {from_event}→{to_event} = {latency_s:.3f}s "
                f"exceeds limit {lt:.3f}s (corr_id={pair[0].corr_id})"
            )
        return latency_s


def _match_order(
    records: list[TraceRecord],
    wanted: list[str],
    same_correlation: bool,
) -> Optional[list[TraceRecord]]:
    """Return the matched records if the wanted event sequence appears
    in order, else None. Helper for :meth:`TraceFeed.expect_order`."""
    if same_correlation:
        # Group by corr_id, look for the sequence within each group.
        by_corr: dict[int, list[TraceRecord]] = {}
        for r in records:
            by_corr.setdefault(r.corr_id, []).append(r)
        for corr_id, group in by_corr.items():
            if corr_id == 0:
                continue  # 0 = "no correlation"; skip
            matched = _walk_sequence(group, wanted)
            if matched is not None:
                return matched
        return None
    return _walk_sequence(records, wanted)


def _walk_sequence(
    records: list[TraceRecord], wanted: list[str]
) -> Optional[list[TraceRecord]]:
    out: list[TraceRecord] = []
    idx = 0
    for r in records:
        if idx < len(wanted) and r.event == wanted[idx]:
            out.append(r)
            idx += 1
            if idx == len(wanted):
                return out
    return None
