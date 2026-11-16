"""SQLAlchemy records and explicit conversion for the frozen receipt domain.

Imperative instrumentation writes _sa_instance_state and cannot directly map
these frozen dataclasses. ORM state stays here; domain classes remain unchanged.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    Numeric,
    Sequence,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.movements import MovementDocument, MovementLine
from maas_ua.domain.receipts import (
    AccountingClassification,
    AssetUnit,
    Money,
    Nomenclature,
    Quantity,
    ReceiptDocument,
    ReceiptLine,
    SerialNumber,
    TrackingPolicy,
)
from maas_ua.domain.write_offs import WriteOffDocument, WriteOffLine


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class NomenclatureRecord(Base):
    __tablename__ = "nomenclatures"
    __table_args__ = (
        UniqueConstraint("code", name="uq_nomenclatures_code"),
        UniqueConstraint("id", "unit_of_measure", name="uq_nomenclatures_id_unit"),
        CheckConstraint("tracking_policy IN ('SERIALIZED', 'QUANTITY')", name="tracking_policy"),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in ("id", "code", "name", "unit_of_measure")
        ),
        CheckConstraint(
            "default_accounting_classification IS NULL OR "
            "(default_accounting_classification <> '' AND "
            "default_accounting_classification = btrim(default_accounting_classification))",
            name="classification_text",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text(collation="C"))
    name: Mapped[str] = mapped_column(Text)
    unit_of_measure: Mapped[str] = mapped_column(Text)
    tracking_policy: Mapped[str] = mapped_column(Text)
    default_accounting_classification: Mapped[str | None] = mapped_column(Text)


def nomenclature_to_record(item: Nomenclature) -> NomenclatureRecord:
    return NomenclatureRecord(**vars(item))


def nomenclature_from_record(record: NomenclatureRecord) -> Nomenclature:
    return Nomenclature(
        record.id,
        TrackingPolicy(record.tracking_policy),
        code=record.code,
        name=record.name,
        unit_of_measure=record.unit_of_measure,
        default_accounting_classification=record.default_accounting_classification,
    )


class ReceiptDocumentRecord(Base):
    __tablename__ = "receipt_documents"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_number: Mapped[str] = mapped_column(Text)
    document_date: Mapped[date]
    effective_date: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)
    lines: Mapped[list[ReceiptLineRecord]] = relationship(
        order_by="ReceiptLineRecord.position", back_populates="receipt"
    )


class ReceiptLineRecord(Base):
    __tablename__ = "receipt_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["nomenclature_id", "quantity_unit"],
            ["nomenclatures.id", "nomenclatures.unit_of_measure"],
            name="fk_receipt_lines_nomenclature_unit",
        ),
        UniqueConstraint("receipt_id", "position", name="uq_receipt_lines_position"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint(
            "accounting_classification IS NULL OR accounting_classification IN ('INVENTORY', 'FIXED_ASSET')",
            name="accounting_classification",
        ),
        CheckConstraint("quantity_count > 0", name="quantity_positive"),
        CheckConstraint("quantity_unit <> '' AND quantity_unit = btrim(quantity_unit)", name="quantity_unit_text"),
        CheckConstraint("tracking_policy = 'SERIALIZED'", name="tracking_serialized"),
        CheckConstraint(
            "unit_price >= 0 AND unit_price < 'Infinity'::numeric",
            name="unit_price_finite_nonnegative",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("receipt_documents.id"))
    position: Mapped[int]
    nomenclature_id: Mapped[str] = mapped_column(Text)
    # Only synchronize identity. The composite database FK validates the stored
    # unit; assigning a relationship must never overwrite it to fit the catalog.
    nomenclature: Mapped[NomenclatureRecord] = relationship(foreign_keys=[nomenclature_id])
    tracking_policy: Mapped[str] = mapped_column(Text)
    accounting_classification: Mapped[str | None] = mapped_column(Text)
    quantity_count: Mapped[int] = mapped_column(BigInteger)
    quantity_unit: Mapped[str] = mapped_column(Text)
    # No precision/scale: the confirmed domain currently performs no quantization.
    unit_price: Mapped[Decimal] = mapped_column(Numeric())
    currency: Mapped[str] = mapped_column(Text)
    analytics: Mapped[list[list[str]]] = mapped_column(JSONB)
    receipt: Mapped[ReceiptDocumentRecord] = relationship(back_populates="lines")


class AssetUnitRecord(Base):
    __tablename__ = "asset_units"
    __table_args__ = (
        UniqueConstraint("serial_namespace", "serial_value", name="uq_asset_units_serial"),
        CheckConstraint(
            "lifecycle_state IS NULL OR lifecycle_state IN ('ACTIVE', 'WRITTEN_OFF')", name="lifecycle_state"
        ),
        CheckConstraint(
            "(initial_location_ref IS NULL AND initial_party_ref IS NULL) OR "
            "(initial_location_ref IS NOT NULL AND initial_party_ref IS NOT NULL "
            "AND initial_location_ref <> '' AND initial_location_ref = btrim(initial_location_ref) "
            "AND initial_party_ref <> '' AND initial_party_ref = btrim(initial_party_ref))",
            name="initial_custody_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    receipt_line_id: Mapped[UUID] = mapped_column(ForeignKey("receipt_lines.id"), index=True)
    # C collation keeps equality exact even if the database default differs.
    serial_namespace: Mapped[str] = mapped_column(Text(collation="C"))
    serial_value: Mapped[str] = mapped_column(Text(collation="C"))
    receipt_line: Mapped[ReceiptLineRecord] = relationship()
    custody_location_ref: Mapped[str | None] = mapped_column(Text)
    custody_party_ref: Mapped[str | None] = mapped_column(Text)
    lifecycle_state: Mapped[str | None] = mapped_column(Text)
    last_effective_date: Mapped[date | None]
    initial_location_ref: Mapped[str | None] = mapped_column(Text)
    initial_party_ref: Mapped[str | None] = mapped_column(Text)


def receipt_to_record(receipt: ReceiptDocument) -> ReceiptDocumentRecord:
    return ReceiptDocumentRecord(
        id=receipt.id,
        document_number=receipt.document_number,
        document_date=receipt.document_date,
        effective_date=receipt.effective_date,
        created_at=receipt.created_at,
        source=receipt.source,
        lines=[
            ReceiptLineRecord(
                id=line.id,
                position=position,
                accounting_classification=line.accounting_classification,
                nomenclature_id=line.nomenclature.id,
                tracking_policy=line.nomenclature.tracking_policy.value,
                quantity_count=line.quantity.count,
                quantity_unit=line.quantity.unit,
                unit_price=line.unit_price.amount,
                currency=line.unit_price.currency,
                analytics=[list(pair) for pair in line.analytics],
            )
            for position, line in enumerate(receipt.lines)
        ],
    )


def receipt_from_record(record: ReceiptDocumentRecord) -> ReceiptDocument:
    return ReceiptDocument(
        id=record.id,
        document_number=record.document_number,
        document_date=record.document_date,
        effective_date=record.effective_date,
        created_at=record.created_at,
        source=record.source,
        lines=tuple(
            ReceiptLine(
                id=line.id,
                nomenclature=nomenclature_from_record(line.nomenclature),
                quantity=Quantity(line.quantity_count, line.quantity_unit),
                unit_price=Money(line.unit_price, line.currency),
                analytics=tuple(tuple(pair) for pair in line.analytics),
                accounting_classification=(
                    AccountingClassification(line.accounting_classification) if line.accounting_classification else None
                ),
            )
            for line in record.lines
        ),
    )


def unit_to_record(unit: AssetUnit) -> AssetUnitRecord:
    return AssetUnitRecord(
        id=unit.id,
        receipt_line_id=unit.receipt_line_id,
        serial_namespace=unit.serial_number.namespace,
        serial_value=unit.serial_number.value,
        custody_location_ref=unit.custody.location_ref if unit.custody else None,
        custody_party_ref=unit.custody.responsible_party_ref if unit.custody else None,
        lifecycle_state=unit.lifecycle_state.value if unit.lifecycle_state else None,
        last_effective_date=unit.last_effective_date,
        initial_location_ref=unit.initial_custody.location_ref if unit.initial_custody else None,
        initial_party_ref=unit.initial_custody.responsible_party_ref if unit.initial_custody else None,
    )


def unit_from_record(record: AssetUnitRecord) -> AssetUnit:
    return AssetUnit(
        id=record.id,
        receipt_line_id=record.receipt_line_id,
        serial_number=SerialNumber(record.serial_namespace, record.serial_value),
        custody=(
            CustodyAssignment(record.custody_location_ref, record.custody_party_ref)
            if record.custody_location_ref is not None
            else None
        ),
        lifecycle_state=LifecycleState(record.lifecycle_state) if record.lifecycle_state else None,
        last_effective_date=record.last_effective_date,
        initial_custody=(
            CustodyAssignment(record.initial_location_ref, record.initial_party_ref)
            if record.initial_location_ref is not None
            else None
        ),
    )


movement_order = Sequence("movement_registration_order", metadata=Base.metadata)


class MovementDocumentRecord(Base):
    __tablename__ = "movement_documents"
    __table_args__ = (
        UniqueConstraint("registration_order", name="uq_movement_documents_registration_order"),
        CheckConstraint("registration_order > 0", name="registration_order_positive"),
        CheckConstraint(
            "source_location_ref <> destination_location_ref OR source_party_ref <> destination_party_ref",
            name="distinct_assignments",
        ),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in (
                "document_number",
                "source_location_ref",
                "source_party_ref",
                "destination_location_ref",
                "destination_party_ref",
            )
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_number: Mapped[str] = mapped_column(Text)
    document_date: Mapped[date]
    effective_date: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Assigned at INSERT/commit, after the command has locked every selected unit.
    # Nullable solely because the migration never invents an order for old rows.
    registration_order: Mapped[int | None] = mapped_column(BigInteger, server_default=movement_order.next_value())
    source_location_ref: Mapped[str] = mapped_column(Text)
    source_party_ref: Mapped[str] = mapped_column(Text)
    destination_location_ref: Mapped[str] = mapped_column(Text)
    destination_party_ref: Mapped[str] = mapped_column(Text)
    lines: Mapped[list[MovementLineRecord]] = relationship(order_by="MovementLineRecord.position")


class MovementLineRecord(Base):
    __tablename__ = "movement_lines"
    __table_args__ = (
        UniqueConstraint("movement_id", "position", name="uq_movement_lines_position"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )
    movement_id: Mapped[UUID] = mapped_column(ForeignKey("movement_documents.id"), primary_key=True)
    asset_unit_id: Mapped[UUID] = mapped_column(ForeignKey("asset_units.id"), primary_key=True, index=True)
    position: Mapped[int]


def movement_to_record(movement: MovementDocument) -> MovementDocumentRecord:
    return MovementDocumentRecord(
        id=movement.id,
        document_number=movement.document_number,
        document_date=movement.document_date,
        effective_date=movement.effective_date,
        created_at=movement.created_at,
        source_location_ref=movement.source.location_ref,
        source_party_ref=movement.source.responsible_party_ref,
        destination_location_ref=movement.destination.location_ref,
        destination_party_ref=movement.destination.responsible_party_ref,
        lines=[
            MovementLineRecord(asset_unit_id=line.asset_unit_id, position=position)
            for position, line in enumerate(movement.lines)
        ],
    )


def movement_from_record(record: MovementDocumentRecord) -> MovementDocument:
    return MovementDocument(
        id=record.id,
        document_number=record.document_number,
        document_date=record.document_date,
        effective_date=record.effective_date,
        created_at=record.created_at,
        source=CustodyAssignment(record.source_location_ref, record.source_party_ref),
        destination=CustodyAssignment(record.destination_location_ref, record.destination_party_ref),
        lines=tuple(MovementLine(line.asset_unit_id) for line in record.lines),
    )


class WriteOffDocumentRecord(Base):
    __tablename__ = "write_off_documents"
    __table_args__ = (
        CheckConstraint("effective_date >= authorization_date", name="acceptance_after_authorization"),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in ("document_number", "authorization_reference")
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_number: Mapped[str] = mapped_column(Text)
    document_date: Mapped[date]
    authorization_reference: Mapped[str] = mapped_column(Text)
    authorization_date: Mapped[date]
    effective_date: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list[WriteOffLineRecord]] = relationship(order_by="WriteOffLineRecord.position")


class WriteOffLineRecord(Base):
    __tablename__ = "write_off_lines"
    __table_args__ = (
        UniqueConstraint("write_off_id", "position", name="uq_write_off_lines_position"),
        UniqueConstraint("asset_unit_id", name="uq_write_off_lines_asset_unit_id"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )
    write_off_id: Mapped[UUID] = mapped_column(ForeignKey("write_off_documents.id"), primary_key=True)
    asset_unit_id: Mapped[UUID] = mapped_column(ForeignKey("asset_units.id"), primary_key=True)
    position: Mapped[int]


def write_off_to_record(document: WriteOffDocument) -> WriteOffDocumentRecord:
    return WriteOffDocumentRecord(
        id=document.id,
        document_number=document.document_number,
        document_date=document.document_date,
        authorization_reference=document.authorization_reference,
        authorization_date=document.authorization_date,
        effective_date=document.effective_date,
        created_at=document.created_at,
        lines=[
            WriteOffLineRecord(asset_unit_id=line.asset_unit_id, position=position)
            for position, line in enumerate(document.lines)
        ],
    )


def write_off_from_record(record: WriteOffDocumentRecord) -> WriteOffDocument:
    return WriteOffDocument(
        id=record.id,
        document_number=record.document_number,
        document_date=record.document_date,
        authorization_reference=record.authorization_reference,
        authorization_date=record.authorization_date,
        effective_date=record.effective_date,
        created_at=record.created_at,
        lines=tuple(WriteOffLine(line.asset_unit_id) for line in record.lines),
    )
