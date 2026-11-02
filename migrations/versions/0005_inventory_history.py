"""Preserve initial custody and future transfer order without fabricating history."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("asset_units", sa.Column("initial_location_ref", sa.Text(), nullable=True))
    op.add_column("asset_units", sa.Column("initial_party_ref", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_asset_units_initial_custody_pair"),
        "asset_units",
        "(initial_location_ref IS NULL AND initial_party_ref IS NULL) OR "
        "(initial_location_ref IS NOT NULL AND initial_party_ref IS NOT NULL "
        "AND initial_location_ref <> '' AND initial_location_ref = btrim(initial_location_ref) "
        "AND initial_party_ref <> '' AND initial_party_ref = btrim(initial_party_ref))",
    )
    op.execute(sa.schema.CreateSequence(sa.Sequence("movement_registration_order")))
    # Adding a volatile default with the column would allocate arbitrary legacy
    # orders. Add it separately so existing facts remain explicitly unknown.
    op.add_column("movement_documents", sa.Column("registration_order", sa.BigInteger(), nullable=True))
    op.alter_column(
        "movement_documents",
        "registration_order",
        server_default=sa.text("nextval('movement_registration_order')"),
    )
    op.create_unique_constraint(
        "uq_movement_documents_registration_order", "movement_documents", ["registration_order"]
    )
    op.create_check_constraint(
        op.f("ck_movement_documents_registration_order_positive"), "movement_documents", "registration_order > 0"
    )


def downgrade():
    raise RuntimeError("0005 is forward-only: dropping initial custody/order would erase audit facts")
