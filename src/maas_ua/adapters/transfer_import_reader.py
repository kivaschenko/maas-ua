"""Set-based exact serial resolution in an isolated read-only snapshot."""

from contextlib import contextmanager

from sqlalchemy import select, text, tuple_

from maas_ua.adapters.orm import AssetUnitRecord, NomenclatureRecord, ReceiptLineRecord, unit_from_record
from maas_ua.adapters.sqlalchemy import SqlAlchemyMovementRepository
from maas_ua.service_layer.transfer_imports import ResolvedTransferAsset, TransferImportFacts


class SqlAlchemyTransferImportReader:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @contextmanager
    def _snapshot(self):
        with self.session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session

    def inspect(self, candidate, movement_id):
        with self._snapshot() as session:
            statement = (
                select(AssetUnitRecord, NomenclatureRecord.code, NomenclatureRecord.name)
                .join(ReceiptLineRecord, AssetUnitRecord.receipt_line_id == ReceiptLineRecord.id)
                .join(NomenclatureRecord, ReceiptLineRecord.nomenclature_id == NomenclatureRecord.id)
                .where(
                    tuple_(AssetUnitRecord.serial_namespace, AssetUnitRecord.serial_value).in_(
                        tuple((row.namespace, row.value) for row in candidate.serials)
                    )
                )
                .order_by(AssetUnitRecord.id)
            )
            assets = tuple(
                ResolvedTransferAsset(unit_from_record(record), code, name)
                for record, code, name in session.execute(statement)
            )
            original = SqlAlchemyMovementRepository(session).get(movement_id)
            return TransferImportFacts(assets, original)
