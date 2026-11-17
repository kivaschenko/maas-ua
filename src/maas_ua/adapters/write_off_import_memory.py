"""Write-off-specific snapshots on the established bounded staging lifecycle."""

from maas_ua.adapters.memory import FakeUnitOfWork
from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.service_layer.write_off_imports import (
    ResolvedWriteOffAsset,
    WriteOffImportFacts,
    WriteOffImportPreview,
)


class MemoryWriteOffPreviewStore(MemoryPreviewStore):
    preview_type = WriteOffImportPreview

    def set_validation(self, preview_id, owner, issues, resolved):
        with self._lock:
            return self._replace(preview_id, owner, issues=issues, resolved=resolved)

    def finish(self, preview_id, owner, result):
        with self._lock:
            # Release advisory snapshots before storing a possibly large result.
            self._replace(preview_id, owner, resolved=())
            super().finish(preview_id, owner, result)


class FakeWriteOffImportReader:
    def __init__(self, store):
        self.store = store

    def inspect(self, candidate, write_off_id):
        wanted = {(row.namespace, row.value) for row in candidate.serials}
        catalogs = {line.id: line for doc in self.store.receipts.values() for line in doc.lines}
        with FakeUnitOfWork(self.store) as uow:
            units = tuple(
                unit
                for unit in self.store.asset_units.values()
                if (unit.serial_number.namespace, unit.serial_number.value) in wanted
            )
            assets = tuple(
                ResolvedWriteOffAsset(
                    facts,
                    catalogs[facts.unit.receipt_line_id].nomenclature.code,
                    catalogs[facts.unit.receipt_line_id].nomenclature.name,
                    catalogs[facts.unit.receipt_line_id].unit_price,
                )
                for facts in uow.asset_units.write_off_facts(units)
            )
        return WriteOffImportFacts(assets, self.store.write_offs.get(write_off_id))
