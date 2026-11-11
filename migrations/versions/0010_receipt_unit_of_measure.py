"""Allow the exact registered nomenclature unit; preserve all receipt facts."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT EXISTS (
        SELECT 1 FROM receipt_lines r JOIN nomenclatures n ON n.id = r.nomenclature_id
        WHERE r.quantity_unit <> n.unit_of_measure
    )""")
    ):
        raise RuntimeError("Receipt/catalog unit mismatches require review; no historical values were rewritten")
    op.create_unique_constraint("uq_nomenclatures_id_unit", "nomenclatures", ["id", "unit_of_measure"])
    op.drop_constraint("fk_receipt_lines_nomenclature_id_nomenclatures", "receipt_lines", type_="foreignkey")
    op.create_foreign_key(
        "fk_receipt_lines_nomenclature_unit",
        "receipt_lines",
        "nomenclatures",
        ["nomenclature_id", "quantity_unit"],
        ["id", "unit_of_measure"],
    )
    op.drop_constraint(op.f("ck_receipt_lines_quantity_unit_pcs"), "receipt_lines", type_="check")
    op.create_check_constraint(
        "quantity_unit_text", "receipt_lines", "quantity_unit <> '' AND quantity_unit = btrim(quantity_unit)"
    )


def downgrade():
    raise RuntimeError("0010 is forward-only; restoring pcs would reject registered historical units")
