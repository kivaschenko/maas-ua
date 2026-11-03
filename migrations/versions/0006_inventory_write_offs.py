"""Receipt classification snapshots and immutable documented inventory write-offs."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # Unknown legacy classification stays unknown; catalog defaults are not evidence.
    op.add_column("receipt_lines", sa.Column("accounting_classification", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_receipt_lines_accounting_classification"),
        "receipt_lines",
        "accounting_classification IS NULL OR accounting_classification IN ('INVENTORY', 'FIXED_ASSET')",
    )
    op.drop_constraint(op.f("ck_asset_units_lifecycle_state"), "asset_units", type_="check")
    op.create_check_constraint(
        op.f("ck_asset_units_lifecycle_state"),
        "asset_units",
        "lifecycle_state IS NULL OR lifecycle_state IN ('ACTIVE', 'WRITTEN_OFF')",
    )
    op.create_table(
        "write_off_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_number", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("authorization_reference", sa.Text(), nullable=False),
        sa.Column("authorization_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effective_date >= authorization_date", name=op.f("ck_write_off_documents_acceptance_after_authorization")
        ),
        *(
            sa.CheckConstraint(
                f"{column} <> '' AND {column} = btrim({column})", name=op.f(f"ck_write_off_documents_{column}_text")
            )
            for column in ("document_number", "authorization_reference")
        ),
    )
    op.create_table(
        "write_off_lines",
        sa.Column("write_off_id", sa.Uuid(), sa.ForeignKey("write_off_documents.id"), primary_key=True),
        sa.Column("asset_unit_id", sa.Uuid(), sa.ForeignKey("asset_units.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("write_off_id", "position", name="uq_write_off_lines_position"),
        sa.UniqueConstraint("asset_unit_id", name="uq_write_off_lines_asset_unit_id"),
        sa.CheckConstraint("position >= 0", name=op.f("ck_write_off_lines_position_nonnegative")),
    )


def downgrade():
    raise RuntimeError("0006 is forward-only: dropping classification/write-offs would erase audit facts")
