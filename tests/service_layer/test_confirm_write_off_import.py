from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Event
from uuid import uuid4

import pytest
from import_fixtures import csv_file as receipt_csv
from write_off_import_fixtures import csv_file, write_off_fields

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.adapters.write_off_import_files import parse_write_off_upload
from maas_ua.adapters.write_off_import_memory import FakeWriteOffImportReader, MemoryWriteOffPreviewStore
from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.service_layer.receipt_imports import PreviewBusy, PreviewConsumed, ReceiptImportCoordinator
from maas_ua.service_layer.write_off_imports import WriteOffImportCoordinator
from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets


@pytest.fixture
def setup():
    catalog = Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
    )
    store = FakeStore(nomenclatures=(catalog,))
    receipts = ReceiptImportCoordinator(
        FakeReceiptImportReader(store), lambda: FakeUnitOfWork(store), MemoryPreviewStore(start_timers=False)
    )
    preview = receipts.preview(parse_receipt_upload(receipt_csv(), "csv"), "a")
    receipts.confirm(preview.id, "a")
    uows = []

    def factory():
        uow = FakeUnitOfWork(store)
        uows.append(uow)
        return uow

    imports = WriteOffImportCoordinator(
        FakeWriteOffImportReader(store), factory, MemoryWriteOffPreviewStore(start_timers=False)
    )
    return store, imports, uows


def stage(imports, **kwargs):
    return imports.preview(parse_write_off_upload(csv_file(**kwargs), "csv"), "a")


def test_duplicate_and_missing_serials_identify_every_row(setup):
    store, imports, uows = setup
    preview = stage(imports, serials=[("EXAMPLE-ISSUER", "EXAMPLE-0000")] * 2 + [("missing", "00001")])
    assert {i.cell.row for i in preview.issues if i.code == "duplicate_serial"} == {2, 3}
    assert {i.cell.row for i in preview.issues if i.code == "missing_serial"} == {4}
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.write_offs


def test_preview_is_advisory_and_reupload_conflicts(setup):
    store, imports, uows = setup
    first, stale = stage(imports), stage(imports)
    imports.confirm(first.id, "a")
    assert imports.confirm(stale.id, "a").status == "rejected"
    assert not stage(imports).ready
    assert len(store.write_offs) == len(uows) == 1


def test_conflicting_uuid_checks_immutable_membership(setup):
    store, imports, _ = setup
    preview = stage(imports)
    candidate = preview.candidate
    ids = tuple(u.id for u in store.asset_units.values())
    write_off_assets(
        WriteOffAssets(
            write_off_id=preview.registration_id,
            document_number=candidate.document_number,
            document_date=candidate.document_date,
            effective_date=candidate.effective_date,
            authorization_reference=candidate.authorization_reference,
            authorization_date=candidate.authorization_date,
            asset_ids=ids[12:24],
        ),
        FakeUnitOfWork(store),
    )
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "write_off_conflict"
    assert len(store.write_offs) == 1


def test_failure_rolls_back_and_retries_same_identity(setup):
    store, imports, _ = setup
    failures = []

    class Failure(FakeUnitOfWork):
        def commit(self):
            failures.append(self)
            raise RuntimeError("synthetic private failure")

    imports.uow_factory = lambda: Failure(store)
    preview = stage(imports)
    assert imports.confirm(preview.id, "a").status == "retryable"
    assert failures[0].rollback_count == 1 and not store.write_offs
    imports.uow_factory = lambda: FakeUnitOfWork(store)
    assert imports.confirm(preview.id, "a").write_off_id == preview.registration_id


def test_uncertain_commit_and_unavailable_reader_recovers_without_second_commit(setup):
    store, imports, _ = setup
    unavailable, commits = [False], []
    inspect = imports.reader.inspect

    def read(*args):
        if unavailable[0]:
            raise RuntimeError("synthetic private read failure")
        return inspect(*args)

    class LostResponse(FakeUnitOfWork):
        def commit(self):
            super().commit()
            commits.append(True)
            unavailable[0] = True
            raise RuntimeError("synthetic private commit failure")

    imports.reader.inspect = read
    imports.uow_factory = lambda: LostResponse(store)
    preview = stage(imports)
    assert imports.confirm(preview.id, "a").status == "retryable"
    unavailable[0] = False
    assert imports.confirm(preview.id, "a").status == "already_registered"
    assert len(commits) == 1


def test_concurrent_confirmation_claims_only_one_writer(setup):
    store, imports, uows = setup
    preview = stage(imports)
    inspect = imports.reader.inspect
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return inspect(*args)

    imports.reader.inspect = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(imports.confirm, preview.id, "a")
        assert entered.wait(5)
        try:
            with pytest.raises(PreviewBusy):
                imports.confirm(preview.id, "a")
        finally:
            release.set()
        assert first.result().status == "registered"
    assert len(uows) == len(store.write_offs) == 1


def test_ambiguous_resolution_is_a_row_error_without_partial_transfer(setup):
    store, imports, uows = setup
    inspect = imports.reader.inspect

    def ambiguous(*args):
        facts = inspect(*args)
        return replace(facts, assets=(*facts.assets, facts.assets[0]))

    imports.reader.inspect = ambiguous
    preview = stage(imports)
    assert {i.cell.row for i in preview.issues if i.code == "ambiguous_serial"} == {2}
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not store.write_offs and not uows


def test_full_resolved_snapshots_count_toward_staging_budget(setup):
    _, imports, _ = setup
    parsed = parse_write_off_upload(csv_file(), "csv")
    raw = imports.previews.create(parsed, "a")
    initial_bytes = imports.previews._entries[raw.id][2]
    imports.previews.discard(raw.id, "a")
    imports.previews.max_bytes = initial_bytes + 1024
    with pytest.raises(PreviewBusy):
        imports.preview(parsed, "a")
    assert not imports.previews._entries


def test_original_header_conflict_cannot_be_reported_as_success(setup):
    store, imports, _ = setup
    preview = stage(imports)
    c = preview.candidate
    write_off_assets(
        WriteOffAssets(
            write_off_id=preview.registration_id,
            document_number="OTHER-DOCUMENT",
            document_date=c.document_date,
            effective_date=c.effective_date,
            authorization_reference=c.authorization_reference,
            authorization_date=c.authorization_date,
            asset_ids=tuple(u.id for u in store.asset_units.values())[:8],
        ),
        FakeUnitOfWork(store),
    )
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "write_off_conflict"


def test_preview_and_commit_preserve_identity_and_allow_mixed_custody(setup):
    from maas_ua.service_layer.movements import TransferAssets, transfer_assets

    store, imports, uows = setup
    original = dict(store.asset_units)
    units = sorted(original.values(), key=lambda u: u.serial_number.value)
    transfer_assets(
        TransferAssets(
            movement_id=uuid4(),
            document_number="EXAMPLE-MOVE",
            document_date=date(2026, 2, 1),
            effective_date=date(2026, 2, 1),
            source=units[0].custody,
            destination=CustodyAssignment("EXAMPLE-OTHER", "EXAMPLE-OTHER"),
            asset_ids=(units[0].id,),
        ),
        FakeUnitOfWork(store),
    )
    preview = stage(imports)
    assert preview.ready and len(preview.resolved) == 8
    assert not store.write_offs and not uows
    result = imports.confirm(preview.id, "a")
    assert result.status == "registered" and result.write_off_id == preview.registration_id
    assert len(uows) == 1 and uows[0].commit_count == 1
    assert len(store.write_offs) == 1
    for key in result.asset_ids:
        after, before = store.asset_units[key], original[key]
        assert after.lifecycle_state is LifecycleState.WRITTEN_OFF
        assert (after.id, after.serial_number, after.receipt_line_id, after.initial_custody) == (
            before.id,
            before.serial_number,
            before.receipt_line_id,
            before.initial_custody,
        )
    with pytest.raises(PreviewConsumed):
        imports.confirm(preview.id, "a")


@pytest.mark.parametrize("classification", [None, "fixed"])
def test_receipt_snapshot_controls_eligibility_without_catalog_inference(setup, classification):
    from maas_ua.domain.receipts import AccountingClassification

    store, imports, uows = setup
    receipt = next(iter(store.receipts.values()))
    line = receipt.lines[0]
    changed = replace(
        line, accounting_classification=(AccountingClassification.FIXED_ASSET if classification else None)
    )
    store._state = replace(store._state, receipts={receipt.id: replace(receipt, lines=(changed,))})
    preview = stage(imports)
    assert len(preview.issues) == 8 and not preview.ready
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.write_offs


@pytest.mark.parametrize(
    "changes",
    [
        {"authorization_date": "2026-02-02"},
        {"effective_date": "2026-01-01", "authorization_date": "2026-01-01"},
    ],
)
def test_authorization_and_receipt_chronology(setup, changes):
    store, imports, uows = setup
    preview = stage(imports, fields=write_off_fields(**changes))
    assert len(preview.issues) == 8
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not store.write_offs and not uows


def test_transfer_after_preview_rechecks_chronology_and_rejects_entire_batch(setup):
    from maas_ua.service_layer.movements import TransferAssets, transfer_assets

    store, imports, uows = setup
    preview = stage(imports)
    unit = next(iter(store.asset_units.values()))
    transfer_assets(
        TransferAssets(
            movement_id=uuid4(),
            document_number="EXAMPLE-LATER",
            document_date=date(2026, 3, 1),
            effective_date=date(2026, 3, 1),
            source=unit.custody,
            destination=CustodyAssignment("EXAMPLE-OTHER", "EXAMPLE-OTHER"),
            asset_ids=(unit.id,),
        ),
        FakeUnitOfWork(store),
    )
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not store.write_offs and not uows


@pytest.mark.parametrize(
    "field,value",
    [
        ("document_number", "OTHER"),
        ("document_date", date(2026, 1, 1)),
        ("authorization_reference", "OTHER"),
        ("authorization_date", date(2026, 1, 1)),
        ("effective_date", date(2026, 2, 2)),
    ],
)
def test_retry_compares_every_immutable_header_fact(setup, field, value):
    store, imports, _ = setup
    preview = stage(imports)
    c = preview.candidate
    command = WriteOffAssets(
        write_off_id=preview.registration_id,
        document_number=c.document_number,
        document_date=c.document_date,
        authorization_reference=c.authorization_reference,
        authorization_date=c.authorization_date,
        effective_date=c.effective_date,
        asset_ids=tuple(u.id for u in store.asset_units.values())[:8],
    )
    write_off_assets(replace(command, **{field: value}), FakeUnitOfWork(store))
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "write_off_conflict"


@pytest.mark.parametrize("change", ["lifecycle", "tracking", "origin", "chronology"])
def test_unknown_or_inconsistent_stored_eligibility_is_not_inferred(setup, change):
    store, imports, uows = setup
    inspect = imports.reader.inspect

    def altered(*args):
        facts = inspect(*args)
        first = facts.assets[0]
        changes = {
            "lifecycle": {"unit": replace(first.unit, lifecycle_state=None)},
            "tracking": {"tracking_policy": None},
            "origin": {"receipt_effective_date": None},
            "chronology": {"unit": replace(first.unit, last_effective_date=date(2026, 1, 17))},
        }
        return replace(facts, assets=(replace(first, facts=replace(first.facts, **changes[change])), *facts.assets[1:]))

    imports.reader.inspect = altered
    preview = stage(imports)
    assert len(preview.issues) == 1 and not preview.ready
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.write_offs


def test_same_day_and_act_date_do_not_introduce_additional_ordering(setup):
    _, imports, _ = setup
    preview = stage(
        imports,
        fields=write_off_fields(
            document_date="2026-03-01", authorization_date="2026-01-16", effective_date="2026-01-16"
        ),
    )
    assert preview.ready
    assert imports.confirm(preview.id, "a").status == "registered"


def test_acquisition_totals_keep_exact_precision_and_currencies_separate(setup):
    from decimal import Decimal, localcontext

    from maas_ua.domain.receipts import Money

    _, imports, _ = setup
    inspect = imports.reader.inspect
    amounts = [
        Money(Decimal("100000000000000000000000000000.00001"), "UAH"),
        Money(Decimal("0.00002"), "UAH"),
        Money(Decimal("1.25"), "USD"),
    ]

    def priced(*args):
        facts = inspect(*args)
        return replace(
            facts, assets=tuple(replace(a, acquisition_price=amounts[i % 3]) for i, a in enumerate(facts.assets))
        )

    imports.reader.inspect = priced
    preview = stage(imports)
    with localcontext() as context:
        context.prec = 5
        result = imports.confirm(preview.id, "a")
    assert result.status == "registered"
    assert result.acquisition_values == (
        Money(Decimal("300000000000000000000000000000.00009"), "UAH"),
        Money(Decimal("2.50"), "USD"),
    )
