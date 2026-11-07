"""Register approved reference snapshots and atomically supersede applicability."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from maas_ua.domain.accounting import (
    AccountingDimensions,
    ChartOfAccounts,
    ChartVersion,
    ChartVersionConflict,
    InvalidAccountingReference,
    SupersededChartVersion,
    supersede_version,
    validate_new_version,
)
from maas_ua.ports import UnitOfWork


def _utc_now() -> datetime:
    return datetime.now(UTC)


def register_chart(chart: ChartOfAccounts, uow: UnitOfWork) -> ChartOfAccounts:
    with uow:
        uow.charts.add_chart(chart)
        uow.commit()
    return chart


def register_chart_version(version: ChartVersion, uow: UnitOfWork) -> ChartVersion:
    with uow:
        if uow.charts.get_chart_for_update(version.chart_id) is None:
            raise InvalidAccountingReference("chart must be registered before its versions")
        validate_new_version(version, uow.charts.versions_for_chart(version.chart_id))
        uow.charts.add_version(version)
        uow.commit()
    return version


@dataclass(frozen=True, kw_only=True)
class SupersedeChartVersion:
    transition_id: UUID
    predecessor_id: UUID
    successor: ChartVersion
    actor_ref: str
    reason: str

    def __post_init__(self):
        if (
            not isinstance(self.transition_id, UUID)
            or not isinstance(self.predecessor_id, UUID)
            or not isinstance(self.successor, ChartVersion)
        ):
            raise InvalidAccountingReference("supersession requires UUIDs and a validated successor snapshot")


def supersede_chart_version(
    command: SupersedeChartVersion,
    uow: UnitOfWork,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> SupersededChartVersion:
    with uow:
        if uow.charts.get_supersession(command.transition_id) is not None:
            raise ChartVersionConflict("supersession identity already registered")
        if uow.charts.get_chart_for_update(command.successor.chart_id) is None:
            raise InvalidAccountingReference("chart must exist")
        if uow.charts.get_supersession(command.transition_id) is not None:
            raise ChartVersionConflict("supersession identity already registered")
        predecessor = uow.charts.get_version(command.predecessor_id)
        if predecessor is None:
            raise InvalidAccountingReference("predecessor must exist")
        result = supersede_version(
            predecessor,
            command.successor,
            id=command.transition_id,
            actor_ref=command.actor_ref,
            reason=command.reason,
            created_at=clock(),
            protected_through=uow.charts.protected_through(predecessor.id),
        )
        remaining = tuple(
            item for item in uow.charts.versions_for_chart(predecessor.chart_id) if item.id != predecessor.id
        )
        validate_new_version(result.successor, (*remaining, result.closed_version))
        uow.charts.apply_supersession(result)
        uow.commit()
    return result


def validate_accounting_dimensions(
    dimensions: AccountingDimensions,
    version_id: UUID,
    uow: UnitOfWork,
) -> AccountingDimensions:
    if not isinstance(dimensions, AccountingDimensions) or not isinstance(version_id, UUID):
        raise InvalidAccountingReference("dimension validation requires typed dimensions and a version UUID")
    with uow:
        version = uow.charts.get_version(version_id)
        if version is None:
            raise InvalidAccountingReference("selected chart version is not registered")
        dimensions.validate_for(version)
    return dimensions
