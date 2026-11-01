"""Persist custody and immutable documented transfer history."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("asset_units", sa.Column("custody_location_ref", sa.Text(), nullable=True))
    op.add_column("asset_units", sa.Column("custody_party_ref", sa.Text(), nullable=True))
    op.add_column("asset_units", sa.Column("lifecycle_state", sa.Text(), nullable=True))
    op.add_column("asset_units", sa.Column("last_effective_date", sa.Date(), nullable=True))
    # Receipt effective dates are known historical facts. Custody and lifecycle
    # are left unknown for existing units; no fictional assignment is introduced.
    op.execute("""UPDATE asset_units a SET last_effective_date = r.effective_date
                  FROM receipt_lines l JOIN receipt_documents r ON r.id = l.receipt_id
                  WHERE a.receipt_line_id = l.id""")
    op.create_check_constraint(
        op.f("ck_asset_units_custody_pair"),
        "asset_units",
        "(custody_location_ref IS NULL AND custody_party_ref IS NULL) OR "
        "(custody_location_ref IS NOT NULL AND custody_party_ref IS NOT NULL "
        "AND custody_location_ref <> '' AND custody_location_ref = btrim(custody_location_ref) "
        "AND custody_party_ref <> '' AND custody_party_ref = btrim(custody_party_ref))",
    )
    op.create_check_constraint(
        op.f("ck_asset_units_lifecycle_state"), "asset_units", "lifecycle_state IS NULL OR lifecycle_state = 'ACTIVE'"
    )
    op.create_table(
        "movement_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_number", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *(
            sa.Column(column, sa.Text(), nullable=False)
            for column in (
                "source_location_ref",
                "source_party_ref",
                "destination_location_ref",
                "destination_party_ref",
            )
        ),
        sa.CheckConstraint(
            "source_location_ref <> destination_location_ref OR source_party_ref <> destination_party_ref",
            name=op.f("ck_movement_documents_distinct_assignments"),
        ),
        *(
            sa.CheckConstraint(
                f"{column} <> '' AND {column} = btrim({column})", name=op.f(f"ck_movement_documents_{column}_text")
            )
            for column in (
                "document_number",
                "source_location_ref",
                "source_party_ref",
                "destination_location_ref",
                "destination_party_ref",
            )
        ),
    )
    op.create_table(
        "movement_lines",
        sa.Column("movement_id", sa.Uuid(), sa.ForeignKey("movement_documents.id"), primary_key=True),
        sa.Column("asset_unit_id", sa.Uuid(), sa.ForeignKey("asset_units.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("movement_id", "position", name="uq_movement_lines_position"),
        sa.CheckConstraint("position >= 0", name=op.f("ck_movement_lines_position_nonnegative")),
    )
    op.create_index("ix_movement_lines_asset_unit_id", "movement_lines", ["asset_unit_id"])


def downgrade():
    op.drop_index("ix_movement_lines_asset_unit_id", table_name="movement_lines")
    op.drop_table("movement_lines")
    op.drop_table("movement_documents")
    op.drop_constraint(op.f("ck_asset_units_lifecycle_state"), "asset_units", type_="check")
    op.drop_constraint(op.f("ck_asset_units_custody_pair"), "asset_units", type_="check")
    for column in ("last_effective_date", "lifecycle_state", "custody_party_ref", "custody_location_ref"):
        op.drop_column("asset_units", column)
