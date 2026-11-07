"""Transactional reference-data fake; shared-store stale commits remain authoritative."""

from datetime import date
from uuid import UUID

from maas_ua.domain.accounting import (
    AccountDefinition,
    ChartOfAccounts,
    ChartSupersession,
    ChartVersion,
    ChartVersionConflict,
    SupersededChartVersion,
    validate_new_version,
)
from maas_ua.ports import RepositoryConflict


class FakeChartRepository:
    def __init__(self, charts, versions, supersessions):
        self._charts = dict(charts)
        self._versions = dict(versions)
        self._supersessions = dict(supersessions)

    def add_chart(self, chart: ChartOfAccounts) -> None:
        if chart.id in self._charts:
            raise ChartVersionConflict("chart identity already registered")
        self._charts[chart.id] = chart

    def get_chart(self, chart_id: UUID) -> ChartOfAccounts | None:
        return self._charts.get(chart_id)

    def get_chart_for_update(self, chart_id: UUID) -> ChartOfAccounts | None:
        return self.get_chart(chart_id)

    def versions_for_chart(self, chart_id: UUID) -> tuple[ChartVersion, ...]:
        return tuple(
            sorted(
                (item for item in self._versions.values() if item.chart_id == chart_id),
                key=lambda item: item.valid_from,
            )
        )

    def add_version(self, version: ChartVersion) -> None:
        if version.chart_id not in self._charts:
            raise RepositoryConflict("chart reference must exist")
        validate_new_version(version, tuple(self._versions.values()))
        existing_ids = {item.id for previous in self._versions.values() for item in previous.accounts}
        if existing_ids & {item.id for item in version.accounts}:
            raise ChartVersionConflict("account definition identity already registered")
        self._versions[version.id] = version

    def get_version(self, version_id: UUID) -> ChartVersion | None:
        return self._versions.get(version_id)

    def get_account(self, account_id: UUID) -> AccountDefinition | None:
        return next(
            (item for version in self._versions.values() for item in version.accounts if item.id == account_id), None
        )

    def version_as_of(self, chart_id: UUID, as_of: date) -> ChartVersion | None:
        matches = [item for item in self.versions_for_chart(chart_id) if item.applies_on(as_of)]
        if len(matches) > 1:
            raise ChartVersionConflict("ambiguous applicable chart versions")
        return matches[0] if matches else None

    def get_supersession(self, transition_id: UUID) -> ChartSupersession | None:
        return self._supersessions.get(transition_id)

    def protected_through(self, version_id: UUID) -> date | None:
        # M6.1 has neither posted financial records nor closed financial periods.
        # Future consumers must compute their protection under the same chart lock.
        return None

    def apply_supersession(self, result: SupersededChartVersion) -> None:
        if result.audit.id in self._supersessions:
            raise ChartVersionConflict("supersession identity already registered")
        if self._versions.get(result.predecessor.id) != result.predecessor:
            raise RepositoryConflict("predecessor changed during supersession")
        self._versions[result.predecessor.id] = result.closed_version
        self.add_version(result.successor)
        self._supersessions[result.audit.id] = result.audit
