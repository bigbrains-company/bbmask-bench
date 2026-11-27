"""Calls the Data Mask service for each document and stores what came back.

One run lives in ``runs/<run-id>/``:

* ``manifest.json``   options, selection and versions (no key, no personal data)
* ``records.jsonl``   one operational record per document (no personal data)
* ``artifacts/``      masked text, placeholder mapping and restored text per document.
                      These contain personal data and stay on this machine.

Metrics are computed later from the artifacts (see :mod:`bbmask_bench.metrics`), so changing
a metric never costs another credit. A run can be resumed: documents that already have a
successful record are skipped, and a document whose anonymize response was received but whose
restore failed is only restored again (restore is free).

Charging, as observed on the service: every anonymize call the service completes is charged,
including calls whose response never reaches the client (client-side 504). A call the service
itself gives up on (its own 504 after 120 s) is not charged. ``restore``, ``get_map`` and
``delete`` are free. A run directory is therefore locked while a process works
on it, so two processes can never pay for the same documents.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from bbmask_bench import __version__
from bbmask_bench.corpus import Document

# 502: engine failed, fee refunded. 503: request never sent. Both are safe to retry.
# 504 is not retried: the request may have been processed and charged.
RETRYABLE_STATUS = frozenset({429, 502, 503})
# Raised by the client itself when the service cannot be reached or does not answer in time.
# Several in a row mean the connection is down, and every further call may be paid for nothing.
CONNECTION_STATUS = frozenset({503, 504})
MAX_ERROR_DETAIL = 200

try:  # POSIX
    import fcntl

    def _acquire_exclusive(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

except ImportError:  # pragma: no cover - Windows
    import msvcrt

    def _acquire_exclusive(handle: Any) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


class RunLocked(RuntimeError):
    """Another process is already working on this run directory."""


# The service publishes its pricing at ``<base_url>/health``: 0.01 credits per started 1,000
# characters of an anonymize call. Used to show an estimate before a run.
PRICE_PER_1K_CHARS = Decimal("0.01")


def estimate_cost(char_counts: Sequence[int]) -> Decimal:
    return sum((PRICE_PER_1K_CHARS * -(-n // 1000) for n in char_counts), Decimal(0))


class MaskClient(Protocol):
    """The part of ``bb_mask.BBAnonym`` the runner uses. Tests pass a fake."""

    def anonymize(self, text: str, persist: bool = True, **options: Any) -> Any: ...
    def restore(self, doc_id_or_state: str | dict, model_text: str) -> str: ...
    def get_map(self, doc_id: str, once: bool = False) -> dict: ...
    def delete(self, doc_id: str) -> bool: ...


@dataclass(frozen=True)
class MaskOptions:
    placeholder_style: str = "angle"
    keep_legal_form: bool = False
    health_data: bool = False
    llm_review: bool = False
    # False: the service stores nothing and returns the mapping in the response.
    persist: bool = False

    def service_kwargs(self) -> dict[str, Any]:
        return {
            "placeholder_style": self.placeholder_style,
            "keep_legal_form": self.keep_legal_form,
            "health_data": self.health_data,
            "llm_review": self.llm_review,
        }


@dataclass
class Record:
    """Operational facts about one document. Safe to publish."""

    doc_id: str
    case_id: str
    kind: str
    size_bucket: str
    n_chars: int
    status: str  # "ok" | "error"
    case_type: str | None = None
    petition_type: str | None = None
    source_format: str | None = None
    attempts: int = 0
    anonymize_ms: float | None = None
    restore_ms: float | None = None
    balance_after: str | None = None
    request_id: str | None = None
    entities_found: dict[str, int] = field(default_factory=dict)
    llm_review: dict | None = None
    error_status: int | None = None
    error_detail: str | None = None
    error_stage: str | None = None
    # Underlying exception class (e.g. ReadTimeout, ConnectError) and how long the failed
    # call ran. Needed to tell a slow service from a broken connection.
    error_type: str | None = None
    error_elapsed_ms: float | None = None
    finished_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _status_of(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None)


def parse_balance(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class RunStore:
    """Files of one run. Appends are serialized so worker threads can share it."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.artifacts_dir = self.run_dir / "artifacts"
        self.records_path = self.run_dir / "records.jsonl"
        self.manifest_path = self.run_dir / "manifest.json"
        self._lock = threading.Lock()

    def prepare(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the run directory exclusively. The OS releases the lock if the process dies."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.run_dir / ".lock").open("a+")
        try:
            try:
                _acquire_exclusive(handle)
            except OSError as exc:
                raise RunLocked(
                    f"Another bbmask-bench process is already working on {self.run_dir}. "
                    "Wait for it to finish; running both would pay twice for the same documents."
                ) from exc
            yield
        finally:
            handle.close()

    def write_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def append_record(self, record: Record) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock, self.records_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_records(self) -> list[dict]:
        """Latest record per document (a resumed run may hold an earlier failure)."""
        if not self.records_path.is_file():
            return []
        latest: dict[str, dict] = {}
        with self.records_path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    latest[row["doc_id"]] = row
        return list(latest.values())

    def artifact_path(self, doc_id: str) -> Path:
        return self.artifacts_dir / f"{doc_id}.json"

    def write_artifact(self, doc_id: str, artifact: dict) -> None:
        self.artifact_path(doc_id).write_text(
            json.dumps(artifact, ensure_ascii=False), encoding="utf-8"
        )

    def read_artifact(self, doc_id: str) -> dict | None:
        path = self.artifact_path(doc_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _call_with_retry(
    fn: Callable[[], Any], retries: int, backoff: float, sleep: Callable[[float], None]
) -> tuple[Any, int]:
    """Run ``fn``; retry only on statuses that are documented as not charged."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except Exception as exc:
            if _status_of(exc) not in RETRYABLE_STATUS or attempt > retries:
                exc.attempts = attempt  # type: ignore[attr-defined]
                raise
            sleep(backoff * 2 ** (attempt - 1))


def process_document(
    client: MaskClient,
    doc: Document,
    options: MaskOptions,
    store: RunStore,
    *,
    retries: int = 2,
    backoff: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Record:
    """Anonymize one document, restore it, and persist record + artifact."""
    text = doc.read_text()
    record = Record(
        doc_id=doc.doc_id,
        case_id=doc.case_id,
        kind=doc.kind,
        size_bucket=doc.size_bucket,
        n_chars=len(text),
        status="error",
        case_type=doc.case_type,
        petition_type=doc.petition_type,
        source_format=doc.source_format,
    )

    def elapsed_ms(since: float) -> float:
        return round((time.perf_counter() - since) * 1000, 1)

    stage = "anonymize"
    started = time.perf_counter()
    try:
        artifact = store.read_artifact(doc.doc_id)
        if artifact and artifact.get("masked_text"):
            # An earlier attempt already paid for anonymize; only the free steps are missing.
            paid = artifact.get("anonymize") or {}
            record.attempts = paid.get("attempts", 0)
            record.anonymize_ms = paid.get("anonymize_ms")
            record.balance_after = paid.get("balance_after")
            record.request_id = paid.get("request_id")
            record.entities_found = paid.get("entities_found") or {}
            record.llm_review = paid.get("llm_review")
        else:
            tagged, record.attempts = _call_with_retry(
                lambda: client.anonymize(text, options.persist, **options.service_kwargs()),
                retries,
                backoff,
                sleep,
            )
            record.anonymize_ms = elapsed_ms(started)
            record.balance_after = tagged.balance_credits
            record.request_id = tagged.request_id
            record.entities_found = dict(tagged.entities_found or {})
            record.llm_review = tagged.llm_review
            artifact = {
                "doc_id": doc.doc_id,
                "source_path": str(doc.path),
                "placeholder_style": tagged.placeholder_style,
                "masked_text": tagged.text,
                "state": tagged.state,
                "service_doc_id": tagged.doc_id,
                "restored_text": None,
                "anonymize": {
                    "attempts": record.attempts,
                    "anonymize_ms": record.anonymize_ms,
                    "balance_after": record.balance_after,
                    "request_id": record.request_id,
                    "entities_found": record.entities_found,
                    "llm_review": record.llm_review,
                },
            }
            # Saved before anything else can fail: this response has been paid for.
            store.write_artifact(doc.doc_id, artifact)

        service_doc_id = artifact.get("service_doc_id")
        if artifact.get("state") is None and service_doc_id:
            stage = "get_map"
            started = time.perf_counter()
            artifact["state"] = client.get_map(service_doc_id)
            store.write_artifact(doc.doc_id, artifact)

        if artifact.get("restored_text") is None:
            # The round trip: what the caller gets back if the model echoes the masked text.
            stage = "restore"
            started = time.perf_counter()
            handle: str | dict = service_doc_id or artifact["state"] or {}
            masked_text = artifact["masked_text"]
            restored, _ = _call_with_retry(
                lambda: client.restore(handle, masked_text), retries, backoff, sleep
            )
            record.restore_ms = elapsed_ms(started)
            artifact["restored_text"] = restored
            store.write_artifact(doc.doc_id, artifact)

        if service_doc_id:
            stage = "delete"
            started = time.perf_counter()
            client.delete(service_doc_id)
            artifact["service_doc_id"] = None

        store.write_artifact(doc.doc_id, artifact)
        record.status = "ok"
    except Exception as exc:
        status = _status_of(exc)
        if status is None:
            raise  # a bug in the benchmark, not a service error
        record.error_status = status
        record.error_stage = stage
        record.error_detail = str(exc)[:MAX_ERROR_DETAIL]
        record.error_type = type(exc.__cause__ or exc).__name__
        record.error_elapsed_ms = elapsed_ms(started)
        record.request_id = getattr(exc, "request_id", None) or record.request_id
        record.attempts = getattr(exc, "attempts", record.attempts) or record.attempts

    record.finished_at = _now()
    store.append_record(record)
    return record


def run(
    client: MaskClient,
    documents: Sequence[Document],
    options: MaskOptions,
    store: RunStore,
    *,
    concurrency: int = 1,
    max_credits: int | None = None,
    retries: int = 2,
    max_consecutive_failures: int | None = 3,
    pause: float = 0.0,
    on_record: Callable[[Record, int, int], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    manifest_extra: dict | None = None,
) -> dict:
    """Process ``documents`` and return a short run summary.

    ``max_credits`` caps the number of anonymize calls started by this invocation. The run
    also stops on HTTP 401 (bad key) and 402 (no balance), since no later call can succeed,
    and after ``max_consecutive_failures`` connection failures in a row (503/504), because a
    call whose response is lost is still charged.

    ``pause`` is a rest, in seconds, after each document. With one request at a time it keeps
    a small server from being busy without a break.

    On Ctrl-C no new call is started; the calls in flight are allowed to finish and are
    recorded, then ``KeyboardInterrupt`` is raised again. ``on_interrupt`` is called first.

    The run directory is locked for the whole call; a second process raises :class:`RunLocked`
    before sending anything.
    """
    with store.lock():
        return _run(
            client,
            documents,
            options,
            store,
            concurrency=concurrency,
            max_credits=max_credits,
            retries=retries,
            max_consecutive_failures=max_consecutive_failures,
            pause=pause,
            on_record=on_record,
            on_interrupt=on_interrupt,
            manifest_extra=manifest_extra,
        )


def _run(
    client: MaskClient,
    documents: Sequence[Document],
    options: MaskOptions,
    store: RunStore,
    *,
    concurrency: int,
    max_credits: int | None,
    retries: int,
    max_consecutive_failures: int | None,
    pause: float,
    on_record: Callable[[Record, int, int], None] | None,
    on_interrupt: Callable[[], None] | None,
    manifest_extra: dict | None,
) -> dict:
    store.prepare()
    done = {r["doc_id"] for r in store.read_records() if r["status"] == "ok"}
    pending = [d for d in documents if d.doc_id not in done]
    already_done = len(documents) - len(pending)
    if max_credits is not None:
        pending = pending[:max_credits]

    manifest = {
        "bench_version": __version__,
        "options": asdict(options),
        "concurrency": concurrency,
        "documents_selected": len(documents),
        "updated_at": _now(),
        **(manifest_extra or {}),
    }
    if store.manifest_path.is_file():
        previous = store.read_manifest()
        if previous.get("options") != manifest["options"]:
            raise ValueError(
                "This run directory was created with different options. "
                "Use a new --run-id to compare option sets."
            )
        manifest["started_at"] = previous.get("started_at", manifest["updated_at"])
    else:
        manifest["started_at"] = manifest["updated_at"]
    store.write_manifest(manifest)

    stop = threading.Event()
    guard = threading.Lock()
    state = {"consecutive": 0, "stop_reason": None}
    counts = {"ok": 0, "error": 0}
    total = len(pending)

    def work(doc: Document) -> Record | None:
        if stop.is_set():
            return None
        record = process_document(client, doc, options, store, retries=retries)
        with guard:
            if record.error_status in CONNECTION_STATUS:
                state["consecutive"] += 1
            else:
                state["consecutive"] = 0
            if record.error_status in (401, 402):
                state["stop_reason"] = "key rejected or balance exhausted"
                stop.set()
            elif max_consecutive_failures and state["consecutive"] >= max_consecutive_failures:
                state["stop_reason"] = (
                    f"{state['consecutive']} connection failures in a row; "
                    "check the network, then resume with the same --run-id"
                )
                stop.set()
        if pause > 0:
            stop.wait(pause)  # returns at once when the run is being stopped
        return record

    finished = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(work, doc) for doc in pending]
        try:
            for future in as_completed(futures):
                record = future.result()
                if record is None:
                    continue
                finished += 1
                counts[record.status] += 1
                if on_record:
                    on_record(record, finished, total)
        except KeyboardInterrupt:
            # Send nothing new. Leaving the ``with`` block waits for the calls in flight:
            # they will be charged, so their results are worth keeping.
            stop.set()
            if on_interrupt:
                on_interrupt()
            raise

    return {
        "skipped_already_done": already_done,
        "attempted": finished,
        "ok": counts["ok"],
        "error": counts["error"],
        "stopped_early": stop.is_set(),
        "stop_reason": state["stop_reason"],
    }
