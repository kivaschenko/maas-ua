"""Persist complete serialized receipts and independently identified units.

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receipt_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_number", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_documents")),
    )
    op.create_table(
        "receipt_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("nomenclature_id", sa.Text(), nullable=False),
        sa.Column("tracking_policy", sa.Text(), nullable=False),
        sa.Column("quantity_count", sa.BigInteger(), nullable=False),
        sa.Column("quantity_unit", sa.Text(), nullable=False),
        sa.Column("unit_price", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("analytics", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_lines")),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["receipt_documents.id"],
            name=op.f("fk_receipt_lines_receipt_id_receipt_documents"),
        ),
        sa.UniqueConstraint("receipt_id", "position", name="uq_receipt_lines_position"),
        sa.CheckConstraint("position >= 0", name=op.f("ck_receipt_lines_position_nonnegative")),
        sa.CheckConstraint("quantity_count > 0", name=op.f("ck_receipt_lines_quantity_positive")),
        sa.CheckConstraint("quantity_unit = 'pcs'", name=op.f("ck_receipt_lines_quantity_unit_pcs")),
        sa.CheckConstraint(
            "tracking_policy = 'SERIALIZED'",
            name=op.f("ck_receipt_lines_tracking_serialized"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0 AND unit_price < 'Infinity'::numeric",
            name=op.f("ck_receipt_lines_unit_price_finite_nonnegative"),
        ),
    )
    op.create_table(
        "asset_units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_line_id", sa.Uuid(), nullable=False),
        sa.Column("serial_namespace", sa.Text(collation="C"), nullable=False),
        sa.Column("serial_value", sa.Text(collation="C"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_units")),
        sa.ForeignKeyConstraint(
            ["receipt_line_id"],
            ["receipt_lines.id"],
            name=op.f("fk_asset_units_receipt_line_id_receipt_lines"),
        ),
        sa.UniqueConstraint("serial_namespace", "serial_value", name="uq_asset_units_serial"),
    )
    op.create_index(op.f("ix_asset_units_receipt_line_id"), "asset_units", ["receipt_line_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_asset_units_receipt_line_id"), table_name="asset_units")
    op.drop_table("asset_units")
    op.drop_table("receipt_lines")
    op.drop_table("receipt_documents")
