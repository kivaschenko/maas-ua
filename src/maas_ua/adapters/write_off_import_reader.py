"""Set-based exact serial resolution in an isolated read-only snapshot."""

from contextlib import contextmanager

from sqlalchemy import select, text, tuple_

from maas_ua.adapters.orm import AssetUnitRecord, NomenclatureRecord, ReceiptLineRecord, unit_from_record
from maas_ua.adapters.sqlalchemy import SqlAlchemyAssetUnitRepository, SqlAlchemyWriteOffRepository
from maas_ua.domain.receipts import Money
from maas_ua.service_layer.write_off_imports import ResolvedWriteOffAsset, WriteOffImportFacts


class SqlAlchemyWriteOffImportReader:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @contextmanager
    def _snapshot(self):
        with self.session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
            yield session

    def inspect(self, candidate, write_off_id):
        with self._snapshot() as session:
            statement = (
                select(
                    AssetUnitRecord,
                    NomenclatureRecord.code,
                    NomenclatureRecord.name,
                    ReceiptLineRecord.unit_price,
                    ReceiptLineRecord.currency,
                )
                .outerjoin(ReceiptLineRecord, AssetUnitRecord.receipt_line_id == ReceiptLineRecord.id)
                .outerjoin(NomenclatureRecord, ReceiptLineRecord.nomenclature_id == NomenclatureRecord.id)
                .where(
                    tuple_(AssetUnitRecord.serial_namespace, AssetUnitRecord.serial_value).in_(
                        tuple((row.namespace, row.value) for row in candidate.serials)
                    )
                )
                .order_by(AssetUnitRecord.id)
            )
            rows = tuple(session.execute(statement))
            facts = SqlAlchemyAssetUnitRepository(session).write_off_facts(tuple(unit_from_record(r) for r, *_ in rows))
            assets = tuple(
                ResolvedWriteOffAsset(
                    fact, code, name, Money(price, currency) if price is not None and currency else None
                )
                for fact, (_, code, name, price, currency) in zip(facts, rows, strict=True)
            )
            original = SqlAlchemyWriteOffRepository(session).get(write_off_id)
            return WriteOffImportFacts(assets, original)
