"""Require reviewed catalog references without rewriting existing receipt IDs."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM receipt_lines r LEFT JOIN nomenclatures n ON n.id = r.nomenclature_id
            WHERE n.id IS NULL OR n.tracking_policy <> r.tracking_policy
                  OR n.unit_of_measure <> r.quantity_unit
        )
    """)
    ):
        raise RuntimeError(
            "Receipt catalog references require reviewed metadata. Upgrade to 0002, "
            "populate nomenclatures with the existing IDs and matching tracking/units, then retry. "
            "No receipt references have been rewritten."
        )
    op.create_foreign_key(
        "fk_receipt_lines_nomenclature_id_nomenclatures", "receipt_lines", "nomenclatures", ["nomenclature_id"], ["id"]
    )


def downgrade():
    op.drop_constraint("fk_receipt_lines_nomenclature_id_nomenclatures", "receipt_lines", type_="foreignkey")
