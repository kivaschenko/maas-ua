"""Transfer-specific snapshots on the established bounded staging lifecycle."""

from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.service_layer.transfer_imports import (
    ResolvedTransferAsset,
    TransferImportFacts,
    TransferImportPreview,
)


class MemoryTransferPreviewStore(MemoryPreviewStore):
    preview_type = TransferImportPreview

    def set_validation(self, preview_id, owner, issues, resolved):
        with self._lock:
            return self._replace(preview_id, owner, issues=issues, resolved=resolved)

    def finish(self, preview_id, owner, result):
        with self._lock:
            # Release advisory snapshots before storing a possibly large result.
            self._replace(preview_id, owner, resolved=())
            super().finish(preview_id, owner, result)


class FakeTransferImportReader:
    def __init__(self, store):
        self.store = store

    def inspect(self, candidate, movement_id):
        wanted = {(row.namespace, row.value) for row in candidate.serials}
        catalogs = {line.id: line.nomenclature for doc in self.store.receipts.values() for line in doc.lines}
        assets = tuple(
            ResolvedTransferAsset(unit, catalogs[unit.receipt_line_id].code, catalogs[unit.receipt_line_id].name)
            for unit in self.store.asset_units.values()
            if (unit.serial_number.namespace, unit.serial_number.value) in wanted
        )
        return TransferImportFacts(assets, self.store.movements.get(movement_id))
