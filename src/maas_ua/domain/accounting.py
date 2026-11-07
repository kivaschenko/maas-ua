"""Approved accounting reference snapshots and controlled applicability changes.

No postings, account correspondence, regulatory seeds or dimension policy engine.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class InvalidAccountingReference(ValueError):
    """Invalid reference metadata, hierarchy, applicability or local mapping."""


class ChartVersionConflict(InvalidAccountingReference):
    """An identity, label or applicability interval is already registered."""


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidAccountingReference("references and codes require nonblank exact text without outer whitespace")


def _id(value):
    if not isinstance(value, UUID):
        raise InvalidAccountingReference("accounting reference identities must be UUIDs")


def _date(value):
    if type(value) is not date:
        raise InvalidAccountingReference("validity boundaries must be business dates")


def _timestamp(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidAccountingReference("registration timestamps must be timezone-aware")


class AccountKind(StrEnum):
    OFFICIAL = "OFFICIAL"
    LOCAL_ANALYTICAL = "LOCAL_ANALYTICAL"


class AccountBasis(StrEnum):
    BALANCE = "BALANCE"
    OFF_BALANCE = "OFF_BALANCE"


@dataclass(frozen=True)
class ChartOfAccounts:
    id: UUID
    accounting_entity_ref: str

    def __post_init__(self):
        _id(self.id)
        _text(self.accounting_entity_ref)


@dataclass(frozen=True, kw_only=True)
class AccountDefinition:
    id: UUID
    code: str
    name: str
    kind: AccountKind
    basis: AccountBasis
    posting_allowed: bool
    authority_reference: str
    parent_id: UUID | None = None
    official_account_id: UUID | None = None

    def __post_init__(self):
        _id(self.id)
        for value in (self.code, self.name, self.authority_reference):
            _text(value)
        if not isinstance(self.kind, AccountKind) or not isinstance(self.basis, AccountBasis):
            raise InvalidAccountingReference("account kind and balance basis must be explicit")
        if type(self.posting_allowed) is not bool:
            raise InvalidAccountingReference("posting eligibility must be a boolean")
        for key in (self.parent_id, self.official_account_id):
            if key is not None:
                _id(key)
        if (self.kind is AccountKind.LOCAL_ANALYTICAL) != (self.official_account_id is not None):
            raise InvalidAccountingReference("only local analytical definitions require an explicit official mapping")


@dataclass(frozen=True, kw_only=True)
class ChartVersion:
    id: UUID
    chart_id: UUID
    label: str
    valid_from: date
    valid_to: date | None
    approval_reference: str
    created_at: datetime
    accounts: tuple[AccountDefinition, ...]

    def __post_init__(self):
        _id(self.id)
        _id(self.chart_id)
        _text(self.label)
        _text(self.approval_reference)
        _date(self.valid_from)
        if self.valid_to is not None:
            _date(self.valid_to)
            if self.valid_to <= self.valid_from:
                raise InvalidAccountingReference("validity must have a positive half-open interval")
        _timestamp(self.created_at)
        accounts = tuple(self.accounts)
        if not accounts or any(not isinstance(item, AccountDefinition) for item in accounts):
            raise InvalidAccountingReference("an approved version requires account definitions")
        by_id = {item.id: item for item in accounts}
        if len(by_id) != len(accounts) or len({item.code for item in accounts}) != len(accounts):
            raise InvalidAccountingReference("duplicate account identity or exact code within version")
        for item in accounts:
            parent = by_id.get(item.parent_id)
            if item.parent_id is not None:
                if parent is None or parent.basis is not item.basis:
                    raise InvalidAccountingReference("parent must exist in the same version and balance basis")
                if item.kind is AccountKind.OFFICIAL and parent.kind is not AccountKind.OFFICIAL:
                    raise InvalidAccountingReference("official accounts cannot descend from local analytical accounts")
            if item.kind is AccountKind.LOCAL_ANALYTICAL:
                official = by_id.get(item.official_account_id)
                if official is None or official.kind is not AccountKind.OFFICIAL or official.basis is not item.basis:
                    raise InvalidAccountingReference(
                        "local mapping must identify an official account of the same basis"
                    )
                if (
                    parent is not None
                    and (parent.id if parent.kind is AccountKind.OFFICIAL else parent.official_account_id)
                    != official.id
                ):
                    raise InvalidAccountingReference("local parent and explicit official mapping disagree")
        # Iterative walks support long approved hierarchies without recursion limits.
        done = set()
        for item in accounts:
            path = set()
            key = item.id
            while key is not None and key not in done:
                if key in path:
                    raise InvalidAccountingReference("account hierarchy contains a cycle")
                path.add(key)
                key = by_id[key].parent_id
            done.update(path)
        object.__setattr__(self, "accounts", accounts)

    def account_by_id(self, account_id: UUID) -> AccountDefinition | None:
        return next((item for item in self.accounts if item.id == account_id), None)

    def account_by_code(self, code: str) -> AccountDefinition | None:
        return next((item for item in self.accounts if item.code == code), None)

    def applies_on(self, as_of: date) -> bool:
        _date(as_of)
        return self.valid_from <= as_of and (self.valid_to is None or as_of < self.valid_to)


def validate_new_version(candidate: ChartVersion, existing: tuple[ChartVersion, ...]) -> None:
    """Validate registration against the locked chart's already approved versions."""
    for previous in existing:
        if previous.id == candidate.id:
            raise ChartVersionConflict("chart version identity already registered")
        if previous.chart_id != candidate.chart_id:
            continue
        if previous.label == candidate.label:
            raise ChartVersionConflict("chart version label already registered")
        if (previous.valid_to is None or candidate.valid_from < previous.valid_to) and (
            candidate.valid_to is None or previous.valid_from < candidate.valid_to
        ):
            raise ChartVersionConflict("applicable chart version intervals overlap")


@dataclass(frozen=True, kw_only=True)
class ChartSupersession:
    id: UUID
    chart_id: UUID
    predecessor_id: UUID
    successor_id: UUID
    effective_date: date
    actor_ref: str
    reason: str
    created_at: datetime

    def __post_init__(self):
        for key in (self.id, self.chart_id, self.predecessor_id, self.successor_id):
            _id(key)
        if self.predecessor_id == self.successor_id:
            raise InvalidAccountingReference("supersession must preserve distinct version identities")
        _date(self.effective_date)
        _text(self.actor_ref)
        _text(self.reason)
        _timestamp(self.created_at)


@dataclass(frozen=True)
class SupersededChartVersion:
    predecessor: ChartVersion
    closed_version: ChartVersion
    successor: ChartVersion
    audit: ChartSupersession

    def __post_init__(self):
        if (
            self.predecessor.valid_to is not None
            or self.successor.chart_id != self.predecessor.chart_id
            or self.successor.valid_from <= self.predecessor.valid_from
            or self.successor.id == self.predecessor.id
            or self.successor.label == self.predecessor.label
            or {item.id for item in self.predecessor.accounts} & {item.id for item in self.successor.accounts}
            or self.closed_version != replace(self.predecessor, valid_to=self.successor.valid_from)
            or (self.audit.chart_id, self.audit.predecessor_id, self.audit.successor_id, self.audit.effective_date)
            != (self.predecessor.chart_id, self.predecessor.id, self.successor.id, self.successor.valid_from)
        ):
            raise InvalidAccountingReference(
                "supersession can change only predecessor applicability and must retain audit links"
            )


def supersede_version(
    predecessor: ChartVersion,
    successor: ChartVersion,
    *,
    id: UUID,
    actor_ref: str,
    reason: str,
    created_at: datetime,
    protected_through: date | None,
) -> SupersededChartVersion:
    if (
        predecessor.chart_id != successor.chart_id
        or predecessor.id == successor.id
        or predecessor.label == successor.label
    ):
        raise InvalidAccountingReference("successor must be a distinct version of the same chart")
    if predecessor.valid_to is not None or successor.valid_from <= predecessor.valid_from:
        raise InvalidAccountingReference("only an open-ended version can be closed at a later successor start")
    if {item.id for item in predecessor.accounts} & {item.id for item in successor.accounts}:
        raise InvalidAccountingReference(
            "successor requires new definition identities; historical identities are retained"
        )
    if protected_through is not None:
        _date(protected_through)
        if successor.valid_from <= protected_through:
            raise InvalidAccountingReference("supersession would invalidate protected posting or period history")
    closed = replace(predecessor, valid_to=successor.valid_from)
    audit = ChartSupersession(
        id=id,
        chart_id=predecessor.chart_id,
        predecessor_id=predecessor.id,
        successor_id=successor.id,
        effective_date=successor.valid_from,
        actor_ref=actor_ref,
        reason=reason,
        created_at=created_at,
    )
    return SupersededChartVersion(predecessor, closed, successor, audit)


@dataclass(frozen=True)
class _DimensionText:
    value: str

    def __post_init__(self):
        _text(self.value)


@dataclass(frozen=True)
class FundRef(_DimensionText):
    pass


@dataclass(frozen=True)
class BudgetProgramCode(_DimensionText):
    pass


@dataclass(frozen=True)
class KEKVCode(_DimensionText):
    pass


@dataclass(frozen=True)
class FundingSourceRef(_DimensionText):
    pass


@dataclass(frozen=True)
class ReportingUnitRef(_DimensionText):
    pass


@dataclass(frozen=True)
class CounterpartyRef(_DimensionText):
    pass


@dataclass(frozen=True, kw_only=True)
class AccountingDimensions:
    fund: FundRef | None = None
    budget_program_code: BudgetProgramCode | None = None
    kekv: KEKVCode | None = None
    funding_source: FundingSourceRef | None = None
    reporting_unit: ReportingUnitRef | None = None
    counterparty: CounterpartyRef | None = None
    local_accounting_analytics: UUID | None = None

    def __post_init__(self):
        for value, expected in (
            (self.fund, FundRef),
            (self.budget_program_code, BudgetProgramCode),
            (self.kekv, KEKVCode),
            (self.funding_source, FundingSourceRef),
            (self.reporting_unit, ReportingUnitRef),
            (self.counterparty, CounterpartyRef),
        ):
            if value is not None and not isinstance(value, expected):
                raise InvalidAccountingReference("financial dimensions require their distinct reference types")
        if self.local_accounting_analytics is not None:
            _id(self.local_accounting_analytics)

    def validate_for(self, version: ChartVersion) -> None:
        if self.local_accounting_analytics is not None:
            account = version.account_by_id(self.local_accounting_analytics)
            if account is None or account.kind is not AccountKind.LOCAL_ANALYTICAL:
                raise InvalidAccountingReference(
                    "local analytics must reference a local definition in the selected version"
                )
