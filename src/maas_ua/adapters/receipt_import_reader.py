"""Read-only original-receipt and serial projections; no import write machinery."""

from contextlib import contextmanager

from sqlalchemy import select, text, tuple_

from maas_ua.adapters.orm import AssetUnitRecord, ReceiptLineRecord, unit_from_record
from maas_ua.adapters.sqlalchemy import SqlAlchemyNomenclatureRepository, SqlAlchemyReceiptRepository
from maas_ua.domain.receipts import RegisteredReceipt, SerialNumber
from maas_ua.service_layer.receipt_imports import MAX_ASSETS, ReceiptImportFacts, SerialMatch


class SqlAlchemyReceiptImportReader:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @contextmanager
    def _snapshot(self):
        with self.session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session
            # Session.close rolls back; never commit or reuse a command session.

    def _receipt(self, session, receipt_id):
        receipt = SqlAlchemyReceiptRepository(session).get(receipt_id)
        if receipt is None:
            return None
        statement = (
            select(AssetUnitRecord)
            .where(AssetUnitRecord.receipt_line_id.in_(tuple(line.id for line in receipt.lines)))
            .order_by(AssetUnitRecord.id)
            .limit(MAX_ASSETS + 1)
        )
        units = tuple(unit_from_record(record) for record in session.scalars(statement))
        return RegisteredReceipt(receipt, units)

    def receipt(self, receipt_id):
        with self._snapshot() as session:
            return self._receipt(session, receipt_id)

    def inspect(self, candidate, registration_id):
        with self._snapshot() as session:
            catalog = SqlAlchemyNomenclatureRepository(session).get_by_code(candidate.nomenclature_code)
            statement = (
                select(
                    AssetUnitRecord.serial_namespace,
                    AssetUnitRecord.serial_value,
                    ReceiptLineRecord.receipt_id,
                )
                .join(ReceiptLineRecord, AssetUnitRecord.receipt_line_id == ReceiptLineRecord.id)
                .where(
                    tuple_(AssetUnitRecord.serial_namespace, AssetUnitRecord.serial_value).in_(
                        tuple((row.namespace, row.value) for row in candidate.serials)
                    )
                )
            )
            matches = tuple(
                SerialMatch(SerialNumber(row.serial_namespace, row.serial_value), row.receipt_id)
                for row in session.execute(statement)
            )
            ids = {registration_id}
            origins = {match.receipt_id for match in matches}
            if len(origins) == 1:
                ids.update(origins)
            originals = tuple(original for key in ids if (original := self._receipt(session, key)) is not None)
            return ReceiptImportFacts(catalog, matches, originals)
