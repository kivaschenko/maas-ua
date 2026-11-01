"""Create catalog; existing installations may stop here to enter reviewed metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nomenclatures",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("code", sa.Text(collation="C"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit_of_measure", sa.Text(), nullable=False),
        sa.Column("tracking_policy", sa.Text(), nullable=False),
        sa.Column("default_accounting_classification", sa.Text(), nullable=True),
        sa.UniqueConstraint("code", name="uq_nomenclatures_code"),
        sa.CheckConstraint(
            "tracking_policy IN ('SERIALIZED', 'QUANTITY')", name=op.f("ck_nomenclatures_tracking_policy")
        ),
        *(
            sa.CheckConstraint(
                f"{column} <> '' AND {column} = btrim({column})", name=op.f(f"ck_nomenclatures_{column}_text")
            )
            for column in ("id", "code", "name", "unit_of_measure")
        ),
        sa.CheckConstraint(
            "default_accounting_classification IS NULL OR "
            "(default_accounting_classification <> '' AND "
            "default_accounting_classification = btrim(default_accounting_classification))",
            name=op.f("ck_nomenclatures_classification_text"),
        ),
    )


def downgrade():
    op.drop_table("nomenclatures")
