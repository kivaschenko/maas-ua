"""Private bounded previews for a single local application process, plus a test reader."""

from dataclasses import is_dataclass, replace
from sys import getsizeof
from threading import RLock, Timer
from time import monotonic
from uuid import uuid4

from maas_ua.domain.receipts import RegisteredReceipt, SerialNumber
from maas_ua.service_layer.receipt_imports import (
    ImportPreview,
    PreviewBusy,
    PreviewConsumed,
    PreviewUnavailable,
    ReceiptImportFacts,
    SerialMatch,
)


def _size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = getsizeof(value)
    if is_dataclass(value):
        size += _size(vars(value), seen)
    elif isinstance(value, dict):
        size += sum(_size(key, seen) + _size(item, seen) for key, item in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(_size(item, seen) for item in value)
    return size


class MemoryPreviewStore:
    preview_type = ImportPreview

    def __init__(
        self,
        *,
        ttl_seconds=1800,
        max_previews=32,
        max_per_session=4,
        max_bytes=32 * 1024 * 1024,
        clock=monotonic,
        start_timers=True,
    ):
        if min(ttl_seconds, max_previews, max_per_session, max_bytes) <= 0:
            raise ValueError("preview limits must be positive")
        self.ttl_seconds, self.max_previews, self.max_per_session = ttl_seconds, max_previews, max_per_session
        self.max_bytes, self.clock, self.start_timers = max_bytes, clock, start_timers
        self._entries, self._timers = {}, {}
        self._lock = RLock()

    def _remove(self, preview_id):
        self._entries.pop(preview_id, None)
        timer = self._timers.pop(preview_id, None)
        if timer:
            timer.cancel()

    def _expire(self):
        for key, (_, preview, _) in list(self._entries.items()):
            if preview.expires_at <= self.clock() and preview.state != "confirming":
                self._remove(key)

    def _timer_expire(self):
        with self._lock:
            self._expire()

    def create(self, parsed, owner):
        with self._lock:
            self._expire()
            preview = self.preview_type(
                uuid4(), uuid4(), parsed.candidate, parsed.issues, self.clock() + self.ttl_seconds
            )
            size = _size(preview) + 2048
            if (
                len(self._entries) >= self.max_previews
                or sum(entry[0] == owner for entry in self._entries.values()) >= self.max_per_session
                or sum(entry[2] for entry in self._entries.values()) + size > self.max_bytes
            ):
                raise PreviewBusy("Preview capacity reached; discard an earlier preview or wait for expiry.")
            self._entries[preview.id] = (owner, preview, size)
            if self.start_timers:
                timer = Timer(self.ttl_seconds, self._timer_expire)
                timer.daemon = True
                self._timers[preview.id] = timer
                timer.start()
            return preview

    def get(self, preview_id, owner):
        with self._lock:
            self._expire()
            entry = self._entries.get(preview_id)
            if entry is None or entry[0] != owner:
                raise PreviewUnavailable("Preview unavailable or expired; upload the file again.")
            return entry[1]

    def _replace(self, preview_id, owner, **changes):
        current = self.get(preview_id, owner)
        updated = replace(current, **changes)
        _, _, previous_size = self._entries[preview_id]
        size = _size(updated) + 2048
        if sum(entry[2] for entry in self._entries.values()) - previous_size + size > self.max_bytes:
            raise PreviewBusy("Preview capacity reached; discard an earlier preview or wait for expiry.")
        self._entries[preview_id] = (owner, updated, size)
        return updated

    def set_issues(self, preview_id, owner, issues):
        with self._lock:
            return self._replace(preview_id, owner, issues=issues)

    def claim(self, preview_id, owner):
        with self._lock:
            preview = self.get(preview_id, owner)
            if preview.state == "confirming":
                raise PreviewBusy("Confirmation is already in progress; check this preview again.")
            if preview.state == "consumed":
                raise PreviewConsumed("This preview was already consumed; view its result.")
            return self._replace(preview_id, owner, state="confirming")

    def finish(self, preview_id, owner, result):
        with self._lock:
            changes = {"state": "ready" if result.status == "retryable" else "consumed", "result": result}
            if result.status != "retryable":
                changes["candidate"] = None
            self._replace(preview_id, owner, **changes)
            self._expire()

    def discard(self, preview_id, owner):
        with self._lock:
            if self.get(preview_id, owner).state == "confirming":
                raise PreviewBusy("Confirmation is in progress.")
            self._remove(preview_id)

    def close(self):
        with self._lock:
            for key in list(self._entries):
                self._remove(key)


class FakeReceiptImportReader:
    def __init__(self, store):
        self.store = store

    def receipt(self, receipt_id):
        receipt = self.store.receipts.get(receipt_id)
        if receipt is None:
            return None
        lines = {line.id for line in receipt.lines}
        return RegisteredReceipt(
            receipt, tuple(unit for unit in self.store.asset_units.values() if unit.receipt_line_id in lines)
        )

    def inspect(self, candidate, registration_id):
        catalog = next(
            (item for item in self.store.nomenclatures.values() if item.code == candidate.nomenclature_code), None
        )
        wanted = {SerialNumber(row.namespace, row.value) for row in candidate.serials}
        lines = {line.id: receipt.id for receipt in self.store.receipts.values() for line in receipt.lines}
        matches = tuple(
            SerialMatch(unit.serial_number, lines[unit.receipt_line_id])
            for unit in self.store.asset_units.values()
            if unit.serial_number in wanted
        )
        ids = {registration_id}
        origins = {match.receipt_id for match in matches}
        if len(origins) == 1:
            ids.update(origins)
        originals = tuple(original for key in ids if (original := self.receipt(key)) is not None)
        return ReceiptImportFacts(catalog, matches, originals)
