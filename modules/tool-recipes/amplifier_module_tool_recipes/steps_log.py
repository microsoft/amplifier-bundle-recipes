"""Append-only per-step run log (``steps.jsonl``).

A recipe session used to persist exactly two files -- ``recipe.yaml`` and
``state.json`` -- and ``state.json`` recorded finished steps as a list of bare
step ids.  So a finished run could say *which* steps completed and nothing
else: not the prompt a step was actually sent after ``{{variable}}``
substitution, not what came back, not when it started or stopped, not whether
it was skipped, retried or timed out.  Once the process exited the run could
not be audited, timed, costed or debugged from what it left behind.

This module is the third artifact.  One ``steps.jsonl`` per session directory,
one JSON object per line, appended as the run goes:

* an ``event: "started"`` line written *before* the step body runs, so a crash
  mid-step leaves evidence that the step was in flight, and
* an ``event: "finished"`` line written when it settles, carrying the terminal
  status, the timing, and the resolved payloads.

The two lines are joined by ``record_id``.

Design constraints, in the order that matters:

1. **It must never break a run.**  Every write is fail-soft: a logging failure
   is swallowed (and logged at debug level) rather than propagated into the
   execution path.  A run that works without a log is strictly better than a
   run that dies because of one.
2. **It must not change ``state.json``.**  Nothing here touches the checkpoint;
   the checkpoint keeps its size discipline and this file carries the detail.
3. **Big payloads are truncated, never silently.**  Any capped field is written
   alongside ``<field>_truncated: true`` and ``<field>_bytes`` (the original
   UTF-8 byte length), so a reader can always tell "this is the whole thing"
   from "this is the first 64 KB of it".
4. **Appends are single-``write`` calls.**  The line is encoded up front and
   written through one ``os.write`` on an ``O_APPEND`` descriptor, so parallel
   ``foreach`` iterations interleave whole records rather than fragments.

Configuration (read at write time, so a test can flip it per-case):

``AMPLIFIER_RECIPE_STEPS_LOG``
    ``0`` / ``false`` / ``no`` / ``off`` disables the log entirely.
``AMPLIFIER_RECIPE_STEPS_LOG_MAX_BYTES``
    Per-field byte cap.  Default 65536 (64 KB).  ``0`` means "no cap".
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STEPS_LOG_FILENAME = "steps.jsonl"

# Bumped when the record shape changes incompatibly.  Every line carries it so
# a reader never has to guess which engine wrote the file.
STEPS_LOG_SCHEMA_VERSION = 1

DEFAULT_MAX_FIELD_BYTES = 64 * 1024

ENV_ENABLED = "AMPLIFIER_RECIPE_STEPS_LOG"
ENV_MAX_FIELD_BYTES = "AMPLIFIER_RECIPE_STEPS_LOG_MAX_BYTES"

_FALSEY = {"0", "false", "no", "off"}

# Terminal statuses.  `started` is an event, not a terminal status, and is
# deliberately absent: a line with `event: "started"` has no `status`.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_RETRIED = "retried"
STATUS_TIMED_OUT = "timed_out"

# Two outcomes that are neither success nor failure.  A run that stopped at an
# approval gate, or was cancelled, did not fail -- calling either one `failed`
# would put a lie in the audit trail, which is the one thing this file exists
# to prevent.
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"

EVENT_STARTED = "started"
EVENT_FINISHED = "finished"

# One line per *process* that appends to a given log, written before that
# process's first step record.  A run says what it did; the header says who did
# it -- which engine file, which git sha, imported from where (recipes-669).
# A resume in a fresh process appends its own header, so a run continued by a
# different engine says so in the file rather than looking seamless.
EVENT_HEADER = "header"

# Logs this process has already announced itself in.  Cheap, and it keeps the
# header at one line per process without ever reading the file back.
_HEADED: set[str] = set()

# Keys written even when their value is None.  A grouping key that is
# sometimes absent forces every consumer to distinguish "this step had no
# parent" from "an older engine wrote this line", and those are not the same
# fact.  Everything else is dropped when None, to keep lines readable.
ALWAYS_PRESENT_KEYS = ("parent_step_id", "iteration")


def steps_log_enabled() -> bool:
    """Whether the per-step log should be written at all."""
    raw = os.environ.get(ENV_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def max_field_bytes() -> int:
    """Per-field byte cap, from the environment or the default.

    ``0`` (or a negative value) means "do not cap".  An unparseable value falls
    back to the default rather than failing the run.
    """
    raw = os.environ.get(ENV_MAX_FIELD_BYTES)
    if raw is None:
        return DEFAULT_MAX_FIELD_BYTES
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.debug(
            "Ignoring unparseable %s=%r; using default %d",
            ENV_MAX_FIELD_BYTES,
            raw,
            DEFAULT_MAX_FIELD_BYTES,
        )
        return DEFAULT_MAX_FIELD_BYTES
    return max(value, 0)


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC timestamp ending in ``Z``."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_record_id() -> str:
    """Short opaque id joining a ``started`` line to its ``finished`` line."""
    return uuid.uuid4().hex[:16]


def coerce_text(value: Any) -> str | None:
    """Render an arbitrary step payload as text for the log.

    ``None`` stays ``None`` (an absent field is meaningfully different from an
    empty one).  Strings pass through.  Anything else is JSON-encoded when it
    can be, and ``repr``-ed when it cannot -- the log records what the run
    actually produced, so an unserialisable object still leaves a trace.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def truncate_text(
    text: str, cap: int, *, keep: str = "head"
) -> tuple[str, bool, int]:
    """Cap ``text`` at ``cap`` UTF-8 bytes.

    Args:
        text: Text to cap.
        cap: Byte ceiling.  ``0`` or less means "no cap".
        keep: ``"head"`` keeps the leading bytes (right for a prompt, where the
            instruction is at the front); ``"tail"`` keeps the trailing bytes
            (right for stdout/stderr, where the failure is at the end).

    Returns:
        ``(text, truncated, original_byte_length)``.  The returned text is
        decoded with ``errors="ignore"`` so a cap landing mid-codepoint drops
        the partial character rather than emitting a replacement one.
    """
    encoded = text.encode("utf-8", errors="replace")
    original = len(encoded)
    if cap <= 0 or original <= cap:
        return text, False, original
    clipped = encoded[-cap:] if keep == "tail" else encoded[:cap]
    return clipped.decode("utf-8", errors="ignore"), True, original


def set_capped_field(
    record: dict[str, Any],
    name: str,
    value: Any,
    *,
    cap: int | None = None,
    keep: str = "head",
) -> None:
    """Write ``value`` into ``record[name]``, capped and honestly marked.

    Sets three keys when the value is present:

    * ``<name>`` -- the (possibly clipped) text,
    * ``<name>_truncated`` -- always present, so a reader never has to infer
      completeness from the absence of a marker, and
    * ``<name>_bytes`` -- the ORIGINAL UTF-8 byte length, so the size of what
      was dropped is recoverable.

    A ``None`` value writes nothing at all.
    """
    text = coerce_text(value)
    if text is None:
        return
    effective_cap = max_field_bytes() if cap is None else cap
    clipped, truncated, original = truncate_text(text, effective_cap, keep=keep)
    record[name] = clipped
    record[f"{name}_truncated"] = truncated
    record[f"{name}_bytes"] = original


def append_record(path: Path, record: dict[str, Any]) -> bool:
    """Append one record to ``path`` as a single JSON line.

    Fail-soft by contract: returns ``False`` and logs at debug level rather
    than raising, because a run must not die because its audit log could not
    be written.

    The line is encoded before the file is opened, and written with one
    ``os.write`` on an ``O_APPEND`` descriptor, so concurrent appends from
    parallel iterations interleave whole lines rather than fragments.
    """
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("steps.jsonl: could not encode record: %s", exc)
        return False

    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        return True
    except Exception as exc:
        logger.debug("steps.jsonl: could not append to %s: %s", path, exc)
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive
                pass


class StepAttempt:
    """One in-flight step attempt: writes its ``started`` and ``finished`` lines.

    Obtained from :meth:`StepLog.begin`.  ``finish`` is idempotent -- calling
    it twice writes one line -- so a caller can close it on both the success
    and the failure path without guarding.
    """

    __slots__ = ("_path", "_base", "_started_at", "_monotonic", "_closed", "record_id")

    def __init__(self, path: Path | None, base: dict[str, Any]) -> None:
        self._path = path
        self._base = base
        self.record_id = base.get("record_id") or new_record_id()
        self._base["record_id"] = self.record_id
        self._started_at = utc_now_iso()
        self._monotonic = time.monotonic()
        self._closed = False

    @property
    def started_at(self) -> str:
        return self._started_at

    def start(self, **fields: Any) -> None:
        """Write the ``started`` line.  Called before the step body runs."""
        if self._path is None:
            return
        record = {
            **self._base,
            "event": EVENT_STARTED,
            "started_at": self._started_at,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        append_record(self._path, record)

    def finish(
        self,
        status: str,
        *,
        error: str | None = None,
        **fields: Any,
    ) -> None:
        """Write the terminal line for this attempt.

        Args:
            status: One of ``completed``/``failed``/``skipped``/``retried``/
                ``timed_out``.
            error: Error text, capped like any other payload.
            **fields: Extra record keys.  ``None`` values are dropped so an
                absent fact is absent rather than recorded as null.
        """
        if self._closed:
            return
        self._closed = True
        if self._path is None:
            return
        finished_at = utc_now_iso()
        duration = round(time.monotonic() - self._monotonic, 3)
        record = {
            **self._base,
            "event": EVENT_FINISHED,
            "status": status,
            "started_at": self._started_at,
            "finished_at": finished_at,
            "duration_s": duration,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        if error is not None:
            set_capped_field(record, "error", error, keep="tail")
        append_record(self._path, record)


class StepLog:
    """Writer bound to one session's ``steps.jsonl``.

    Construct with ``None`` (or with the log disabled) to get an inert writer:
    every method is then a no-op, so callers never need to branch.
    """

    __slots__ = ("path",)

    def __init__(self, path: Path | None) -> None:
        self.path = path if (path is not None and steps_log_enabled()) else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def ensure_header(self) -> bool:
        """Write this process's ``header`` line, once per log file.

        Fail-soft like every other write here, and idempotent per process: the
        first ``StepLog`` opened against a path writes it, every later one is a
        no-op.  Returns ``True`` only when a line was actually appended.
        """
        if self.path is None:
            return False
        key = str(self.path)
        if key in _HEADED:
            return False
        _HEADED.add(key)
        try:
            from .engine_provenance import engine_provenance

            engine: dict[str, Any] | None = engine_provenance()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("steps.jsonl: engine provenance unavailable: %s", exc)
            engine = None
        record = {
            "v": STEPS_LOG_SCHEMA_VERSION,
            "record_id": new_record_id(),
            "event": EVENT_HEADER,
            "written_at": utc_now_iso(),
            "pid": os.getpid(),
            "engine": engine,
        }
        return append_record(self.path, record)

    def begin(self, **base: Any) -> StepAttempt:
        """Open an attempt.  Does not write anything until ``start`` is called."""
        cleaned = {
            k: v
            for k, v in base.items()
            if v is not None or k in ALWAYS_PRESENT_KEYS
        }
        cleaned.setdefault("v", STEPS_LOG_SCHEMA_VERSION)
        return StepAttempt(self.path, cleaned)

    def record(self, status: str, **base: Any) -> None:
        """Write a single settled record with no in-flight phase.

        Used for outcomes that never had a body to run -- a step whose
        condition evaluated false, for instance -- where a ``started`` line
        would imply work that did not happen.
        """
        if self.path is None:
            return
        record = {
            "v": STEPS_LOG_SCHEMA_VERSION,
            "record_id": new_record_id(),
            "event": EVENT_FINISHED,
            "status": status,
            "started_at": utc_now_iso(),
        }
        record.update(
            {
                k: v
                for k, v in base.items()
                if v is not None or k in ALWAYS_PRESENT_KEYS
            }
        )
        record.setdefault("duration_s", 0.0)
        append_record(self.path, record)


def read_steps_log(session_dir: Path) -> list[dict[str, Any]]:
    """Read a session's ``steps.jsonl`` back into records.

    Malformed lines are skipped rather than raising -- the file is append-only
    and a crashed run can leave a half-written final line.
    """
    path = Path(session_dir) / STEPS_LOG_FILENAME
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return records
