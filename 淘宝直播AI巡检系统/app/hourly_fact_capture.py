"""Clock-driven persistence for auditable hourly Taobao facts.

This module never controls recording, transcription, analysis, or delivery.
It only schedules short screen-total reads inside the existing ±5 minute
boundary tolerance and writes them to SQLite for later aggregation.
"""

from __future__ import annotations

import time
from typing import Callable

from .db import HOURLY_FACT_BOUNDARY_FIELDS
from .recorder.mtop import MtopAuthError


HOUR_MS = 3_600_000
PREGRAB_MS = 120_000
BOUNDARY_TOLERANCE_MS = 300_000


def _ensure_boundary_job(
        store, *, business_session_key: str, boundary_kind: str,
        nominal_boundary_ms: int, live_id: str, stream_id: int | None,
        next_attempt_ms: int) -> str:
    nominal_ms = int(nominal_boundary_ms)
    return store.ensure_hourly_fact_capture_job(
        business_session_key=str(business_session_key),
        boundary_kind=str(boundary_kind),
        nominal_boundary_ms=nominal_ms,
        live_id=str(live_id),
        stream_id=stream_id,
        next_attempt_at=int(next_attempt_ms) / 1000.0,
        expires_at=(nominal_ms + BOUNDARY_TOLERANCE_MS) / 1000.0,
    )


def _schedule_clock_jobs(
        store, *, business_session_key: str, live_id: str,
        stream_id: int | None, observed_at_ms: int) -> list[str]:
    observed_ms = int(observed_at_ms)
    jobs: list[str] = []
    floor_ms = observed_ms - observed_ms % HOUR_MS
    if 0 <= observed_ms - floor_ms <= BOUNDARY_TOLERANCE_MS:
        jobs.append(_ensure_boundary_job(
            store,
            business_session_key=str(business_session_key),
            boundary_kind="clock",
            nominal_boundary_ms=floor_ms,
            live_id=str(live_id),
            stream_id=stream_id,
            next_attempt_ms=observed_ms,
        ))
    next_hour_ms = floor_ms + HOUR_MS
    if 0 <= next_hour_ms - observed_ms <= PREGRAB_MS:
        jobs.append(_ensure_boundary_job(
            store,
            business_session_key=str(business_session_key),
            boundary_kind="clock",
            nominal_boundary_ms=next_hour_ms,
            live_id=str(live_id),
            stream_id=stream_id,
            next_attempt_ms=observed_ms,
        ))
    return jobs


def schedule_persisted_open_sources(
        store, *, observed_at_ms: int,
        earliest_start: str = "05:30", latest_end: str = "01:30",
) -> tuple[str, ...]:
    """Schedule clocks for lifecycle sources that survived a process restart.

    This deliberately does not refresh ``last_observed_at_ms``: a persisted
    open source remains required evidence during a room-probe outage, but the
    outage itself is not a new verified-live observation.
    """
    jobs: list[str] = []
    rows = store.query(
        """SELECT business_session_key,live_id,stream_id
           FROM business_fact_source_intervals WHERE status='open'
           ORDER BY business_session_key,id"""
    )
    for row in rows:
        from .business_facts import business_operating_bounds
        try:
            bound_start_ms, bound_end_ms = business_operating_bounds(
                str(row["business_session_key"]),
                earliest_start=str(earliest_start),
                latest_end=str(latest_end),
            )
        except ValueError:
            continue
        if not bound_start_ms <= int(observed_at_ms) <= bound_end_ms:
            continue
        jobs.extend(_schedule_clock_jobs(
            store,
            business_session_key=str(row["business_session_key"]),
            live_id=str(row["live_id"]),
            stream_id=row["stream_id"],
            observed_at_ms=int(observed_at_ms),
        ))
    return tuple(dict.fromkeys(jobs))


def schedule_active_source(
        store, *, business_session_key: str, live_id: str,
        stream_id: int | None, observed_at_ms: int,
        source_started_at_ms: int | None = None) -> dict:
    """Observe one verified live source and schedule nearby fact boundaries."""
    observed_ms = int(observed_at_ms)
    transition = store.observe_business_fact_source(
        str(business_session_key), live_id=str(live_id), stream_id=stream_id,
        observed_at_ms=observed_ms,
        source_started_at_ms=source_started_at_ms,
    )
    jobs: list[str] = []
    opened = transition.get("opened")
    if opened:
        jobs.append(_ensure_boundary_job(
            store,
            business_session_key=str(business_session_key),
            boundary_kind="live_start",
            nominal_boundary_ms=int(opened["started_at_ms"]),
            live_id=str(opened["live_id"]),
            stream_id=opened.get("stream_id"),
            next_attempt_ms=min(
                observed_ms,
                int(opened["started_at_ms"]) + BOUNDARY_TOLERANCE_MS,
            ),
        ))
    closed = transition.get("closed")
    if closed:
        jobs.append(_ensure_boundary_job(
            store,
            business_session_key=str(business_session_key),
            boundary_kind="live_end",
            nominal_boundary_ms=int(closed["ended_at_ms"]),
            live_id=str(closed["live_id"]),
            stream_id=closed.get("stream_id"),
            next_attempt_ms=observed_ms,
        ))

    jobs.extend(_schedule_clock_jobs(
        store,
        business_session_key=str(business_session_key),
        live_id=str(live_id),
        stream_id=stream_id,
        observed_at_ms=observed_ms,
    ))
    return {"transition": transition, "job_keys": tuple(dict.fromkeys(jobs))}


def schedule_source_end(
        store, *, business_session_key: str, live_id: str,
        stream_id: int | None, ended_at_ms: int,
        observed_at_ms: int) -> str | None:
    """Close one confirmed live source and preserve its final cumulative fact."""
    closed = store.close_business_fact_source(
        str(business_session_key), live_id=str(live_id),
        ended_at_ms=int(ended_at_ms), end_evidence="verified_end",
    )
    if closed is None:
        return None
    nominal_ms = int(closed["ended_at_ms"])
    return _ensure_boundary_job(
        store,
        business_session_key=str(business_session_key),
        boundary_kind="live_end",
        nominal_boundary_ms=nominal_ms,
        live_id=str(live_id),
        stream_id=stream_id,
        next_attempt_ms=min(
            int(observed_at_ms), nominal_ms + BOUNDARY_TOLERANCE_MS),
    )


def capture_due_boundary(
        cfg: dict, store, *, now: float | None = None,
        fetcher: Callable[[dict, str], dict] | None = None) -> bool:
    """Claim and execute at most one due request; return whether work ran."""
    fixed_clock = now is not None
    checked_at = float(time.time() if now is None else now)
    store.finalize_expired_hourly_fact_capture_jobs(now=checked_at)
    claimed = store.claim_hourly_fact_capture_jobs(
        now=checked_at, limit=1, lease_sec=90)
    if not claimed:
        return False
    job = dict(claimed[0])
    key = str(job["job_key"])
    if checked_at > float(job["expires_at"]):
        store.finalize_expired_hourly_fact_capture_jobs(now=checked_at)
        return False
    if fetcher is None:
        from .metrics.qianniu import fetch_screen_totals

        def fetcher(fetch_cfg: dict, live_id: str) -> dict:
            # This worker participates in a five-minute persisted retry window;
            # one attempt must stay short enough for launchd's controlled stop.
            return fetch_screen_totals(
                fetch_cfg, live_id, request_timeout=2.0,
                network_retries=0, lock_timeout=0.25)
    success = False
    error = ""
    recoverable_until_expiry = False
    completed_at = checked_at
    try:
        snapshot = fetcher(cfg, str(job["live_id"]))
        completed_at = checked_at if fixed_clock else float(time.time())
        success = bool(
            isinstance(snapshot, dict)
            and snapshot.get("source") == "screen.totalStats"
            and snapshot.get("data_state") == "ok"
            and all(snapshot.get(name) is not None
                    for name in HOURLY_FACT_BOUNDARY_FIELDS)
        )
        if success and completed_at <= float(job["expires_at"]):
            store.save_hourly_fact_boundary(
                key, snapshot, captured_at_ms=int(completed_at * 1000))
        elif success:
            success = False
            error = "boundary response completed after truth window"
        else:
            error = "empty or invalid screen boundary"
    except MtopAuthError:
        completed_at = checked_at if fixed_clock else float(time.time())
        error = "Taobao authentication unavailable"
        recoverable_until_expiry = True
    except Exception as exc:  # network/provider errors are retried inside the truth window
        completed_at = checked_at if fixed_clock else float(time.time())
        error = type(exc).__name__
    store.finish_hourly_fact_capture_attempt(
        key, now=completed_at, success=success, error=error,
        recoverable_until_expiry=recoverable_until_expiry)
    return True


def boundary_work_due(store, *, now: float | None = None) -> bool:
    """Cheap main-thread check used before submitting the network worker."""
    checked_at = float(time.time() if now is None else now)
    store.finalize_expired_hourly_fact_capture_jobs(now=checked_at)
    rows = store.query(
        """SELECT 1 FROM hourly_fact_capture_jobs
           WHERE ((status='pending') OR
                  (status='running' AND lease_until<=?))
             AND next_attempt_at<=? AND expires_at>=? LIMIT 1""",
        (checked_at, checked_at, checked_at),
    )
    return bool(rows)


__all__ = [
    "BOUNDARY_TOLERANCE_MS",
    "boundary_work_due",
    "capture_due_boundary",
    "schedule_active_source",
    "schedule_persisted_open_sources",
    "schedule_source_end",
]
