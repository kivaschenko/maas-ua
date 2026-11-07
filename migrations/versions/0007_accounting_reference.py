"""Approved accounting charts, immutable definitions and audited supersession."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ExcludeConstraint

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _text_checks(table, columns):
    return tuple(
        sa.CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=op.f(f"ck_{table}_{column}_text"))
        for column in columns
    )


def upgrade():
    # PostgreSQL's trusted btree_gist extension supplies UUID equality for the
    # exclusion constraint. No account definitions or business codes are seeded.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "accounting_charts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("accounting_entity_ref", sa.Text(collation="C"), nullable=False),
        sa.CheckConstraint(
            "accounting_entity_ref <> '' AND accounting_entity_ref = btrim(accounting_entity_ref)",
            name=op.f("ck_accounting_charts_entity_text"),
        ),
    )
    op.create_table(
        "accounting_chart_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chart_id", sa.Uuid(), sa.ForeignKey("accounting_charts.id"), nullable=False),
        sa.Column("label", sa.Text(collation="C"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("approval_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chart_id", "label", name="uq_accounting_chart_versions_label"),
        sa.UniqueConstraint("chart_id", "id", name="uq_accounting_chart_versions_chart_id"),
        sa.CheckConstraint(
            "isfinite(valid_from) AND (valid_to IS NULL OR (isfinite(valid_to) AND valid_to > valid_from))",
            name=op.f("ck_accounting_chart_versions_validity"),
        ),
        *_text_checks("accounting_chart_versions", ("label", "approval_reference")),
        ExcludeConstraint(
            ("chart_id", "="),
            (sa.text("daterange(valid_from, valid_to, '[)')"), "&&"),
            name="ex_accounting_chart_versions_validity",
            using="gist",
        ),
    )
    op.create_table(
        "accounting_account_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("accounting_chart_versions.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(collation="C"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("posting_allowed", sa.Boolean(), nullable=False),
        sa.Column("authority_reference", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("official_account_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("version_id", "code", name="uq_accounting_account_definitions_code"),
        sa.UniqueConstraint("version_id", "position", name="uq_accounting_account_definitions_position"),
        sa.UniqueConstraint("version_id", "id", name="uq_accounting_account_definitions_version_id"),
        sa.ForeignKeyConstraint(
            ("version_id", "parent_id"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_accounts_parent_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ("version_id", "official_account_id"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_accounts_official_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "kind IN ('OFFICIAL', 'LOCAL_ANALYTICAL')", name=op.f("ck_accounting_account_definitions_kind")
        ),
        sa.CheckConstraint("basis IN ('BALANCE', 'OFF_BALANCE')", name=op.f("ck_accounting_account_definitions_basis")),
        sa.CheckConstraint(
            "(kind='OFFICIAL' AND official_account_id IS NULL) OR "
            "(kind='LOCAL_ANALYTICAL' AND official_account_id IS NOT NULL)",
            name=op.f("ck_accounting_account_definitions_mapping_required"),
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_accounting_account_definitions_parent_not_self")
        ),
        sa.CheckConstraint(
            "official_account_id IS NULL OR official_account_id <> id",
            name=op.f("ck_accounting_account_definitions_mapping_not_self"),
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_accounting_account_definitions_position_nonnegative")),
        *_text_checks("accounting_account_definitions", ("code", "name", "authority_reference")),
    )
    op.create_table(
        "accounting_chart_supersessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chart_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_id", sa.Uuid(), nullable=False),
        sa.Column("successor_id", sa.Uuid(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ("chart_id", "predecessor_id"),
            ("accounting_chart_versions.chart_id", "accounting_chart_versions.id"),
            name="fk_accounting_supersessions_predecessor_chart",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ("chart_id", "successor_id"),
            ("accounting_chart_versions.chart_id", "accounting_chart_versions.id"),
            name="fk_accounting_supersessions_successor_chart",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("predecessor_id", name="uq_accounting_chart_supersessions_predecessor"),
        sa.UniqueConstraint("successor_id", name="uq_accounting_chart_supersessions_successor"),
        sa.CheckConstraint(
            "predecessor_id <> successor_id", name=op.f("ck_accounting_chart_supersessions_distinct_versions")
        ),
        *_text_checks("accounting_chart_supersessions", ("actor_ref", "reason")),
    )


def downgrade():
    raise RuntimeError(
        "0007 is forward-only: dropping accounting definitions or supersession evidence would erase history"
    )
