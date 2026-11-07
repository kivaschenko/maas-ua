from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from threading import Event
from uuid import uuid4
from weakref import ref

import pytest
from import_fixtures import csv_file, document_fields, xlsx_file

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import AccountingClassification, Nomenclature, TrackingPolicy
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipt_imports import (
    PreviewBusy,
    PreviewConsumed,
    PreviewUnavailable,
    ReceiptImportCoordinator,
    validate_import_candidate,
)
from maas_ua.service_layer.receipts import register_inventory_receipt


@pytest.fixture
def catalog():
    return Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
    )


@pytest.fixture
def candidate():
    return parse_receipt_upload(csv_file(), "csv").candidate


@pytest.fixture
def setup(catalog):
    store = FakeStore(nomenclatures=(catalog,))
    previews = MemoryPreviewStore(start_timers=False)
    reader = FakeReceiptImportReader(store)
    uows = []

    def factory():
        uow = FakeUnitOfWork(store)
        uows.append(uow)
        return uow

    return store, ReceiptImportCoordinator(reader, factory, previews), uows


def test_preview_then_explicit_confirmation_commits_once(setup):
    store, imports, uows = setup
    preview = imports.preview(parse_receipt_upload(xlsx_file(), "xlsx"), "session-a")
    assert preview.ready and not preview.issues
    assert not store.receipts and not store.asset_units and not uows
    result = imports.confirm(preview.id, "session-a")
    assert result.status == "registered"
    assert result.receipt_id == preview.registration_id
    assert len(store.asset_units) == 27
    assert uows[0].commit_count == 1
    assert imports.receipt(preview.id, "session-a").receipt.id == result.receipt_id
    with pytest.raises(PreviewConsumed):
        imports.confirm(preview.id, "session-a")
    assert len(uows) == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"quantity": 26}, "quantity_mismatch"),
        ({"quantity": 0}, "invalid_quantity"),
        ({"line_total": Decimal("1")}, "total_mismatch"),
        ({"classification": AccountingClassification.FIXED_ASSET}, "inventory_required"),
        ({"location_ref": ""}, "custody_required"),
        ({"responsible_party_ref": ""}, "custody_required"),
    ],
)
def test_shared_business_rules_reject_entire_candidate(candidate, catalog, changes, code):
    validated = validate_import_candidate(replace(candidate, **changes), catalog)
    assert validated.command is None
    assert code in {issue.code for issue in validated.issues}


def test_duplicate_errors_identify_all_rows_without_exposing_values(candidate, catalog):
    duplicate = replace(candidate.serials[1], value=candidate.serials[0].value)
    candidate = replace(candidate, serials=(candidate.serials[0], duplicate, *candidate.serials[2:]))
    result = validate_import_candidate(candidate, catalog)
    errors = [issue for issue in result.issues if issue.code == "duplicate_serial"]
    assert {issue.cell.row for issue in errors} == {2, 3}
    assert all(candidate.serials[0].value not in issue.message for issue in errors)


def test_catalog_must_exist_and_be_serialized_in_pcs(candidate, catalog):
    for invalid in (
        None,
        replace(catalog, tracking_policy=TrackingPolicy.QUANTITY),
        replace(catalog, unit_of_measure="kg"),
    ):
        assert validate_import_candidate(candidate, invalid).command is None


def test_exact_money_validation_ignores_ambient_context(candidate, catalog):
    candidate = replace(candidate, unit_price=Decimal("0.12345"), line_total=Decimal("3.33315"))
    with localcontext() as context:
        context.prec = 3
        assert validate_import_candidate(candidate, catalog).command is not None


def test_same_value_different_namespace_and_case_remain_distinct(candidate, catalog):
    rows = (
        replace(candidate.serials[0], value="abc"),
        replace(candidate.serials[1], value="ABC"),
        replace(candidate.serials[2], namespace="OTHER", value="abc"),
        *candidate.serials[3:],
    )
    assert not validate_import_candidate(replace(candidate, serials=rows), catalog).issues


def test_invalid_preview_cannot_register(setup):
    store, imports, uows = setup
    preview = imports.preview(parse_receipt_upload(csv_file(fields=document_fields(line_quantity="26")), "csv"), "a")
    assert not preview.ready
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not store.receipts and not uows


def test_catalog_and_serial_state_are_rechecked_on_confirm(setup, catalog):
    store, imports, uows = setup
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    other = validate_import_candidate(
        parse_receipt_upload(csv_file(fields=document_fields(source="OTHER")), "csv").candidate, catalog
    )
    register_inventory_receipt(other.command, FakeUnitOfWork(store))
    outcome = imports.confirm(preview.id, "a")
    assert outcome.status == "rejected"
    assert {issue.cell.row for issue in outcome.issues if issue.code == "serial_conflict"} == set(range(2, 29))
    assert not uows
    assert len(store.receipts) == 1


def test_reupload_identical_or_equivalent_file_returns_original_receipt(setup):
    store, imports, uows = setup
    first = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    original = imports.confirm(first.id, "a")
    second = imports.preview(parse_receipt_upload(xlsx_file(), "xlsx"), "a")
    outcome = imports.confirm(second.id, "a")
    assert outcome.status == "already_registered"
    assert outcome.receipt_id == original.receipt_id
    assert len(uows) == 1 and len(store.asset_units) == 27


def test_existing_receipt_matches_after_transfer_and_process_restart(setup):
    store, imports, _ = setup
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    original = imports.confirm(preview.id, "a")
    unit = next(iter(store.asset_units.values()))
    transfer_assets(
        TransferAssets(
            movement_id=uuid4(),
            document_number="EXAMPLE-MOVE",
            document_date=date(2026, 2, 1),
            effective_date=date(2026, 2, 1),
            source=unit.custody,
            destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
            asset_ids=(unit.id,),
        ),
        FakeUnitOfWork(store),
    )
    fresh = ReceiptImportCoordinator(
        FakeReceiptImportReader(store), lambda: FakeUnitOfWork(store), MemoryPreviewStore(start_timers=False)
    )
    again = fresh.preview(parse_receipt_upload(csv_file(), "csv"), "new-session")
    assert fresh.confirm(again.id, "new-session").receipt_id == original.receipt_id
    assert len(store.asset_units) == 27


def test_matching_document_number_with_disjoint_serials_is_allowed(setup):
    store, imports, _ = setup
    first = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    imports.confirm(first.id, "a")
    second = imports.preview(parse_receipt_upload(csv_file(serials=[("OTHER", str(n)) for n in range(27)]), "csv"), "a")
    assert imports.confirm(second.id, "a").status == "registered"
    assert len(store.receipts) == 2


def test_expiry_owner_and_capacity_are_enforced(setup):
    _, imports, _ = setup
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    with pytest.raises(PreviewUnavailable):
        imports.confirm(preview.id, "b")
    now = [0]
    imports.previews = MemoryPreviewStore(ttl_seconds=10, max_previews=1, clock=lambda: now[0], start_timers=False)
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    with pytest.raises(PreviewBusy):
        imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    now[0] = 11
    with pytest.raises(PreviewUnavailable):
        imports.confirm(preview.id, "a")
    assert imports.preview(parse_receipt_upload(csv_file(), "csv"), "a").ready


def test_concurrent_same_preview_has_only_one_writer(setup):
    store, imports, uows = setup
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    entered, release = Event(), Event()
    original_inspect = imports.reader.inspect

    def slow_inspect(*args):
        entered.set()
        assert release.wait(5)
        return original_inspect(*args)

    imports.reader.inspect = slow_inspect
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(imports.confirm, preview.id, "a")
        assert entered.wait(5)
        try:
            with pytest.raises(PreviewBusy):
                imports.confirm(preview.id, "a")
        finally:
            release.set()
        assert first.result().status == "registered"
    assert len(uows) == 1 and len(store.asset_units) == 27


def test_uncertain_commit_is_reconciled_without_new_registration(setup):
    store, imports, _ = setup

    class LostResponse(FakeUnitOfWork):
        def commit(self):
            super().commit()
            raise RuntimeError("synthetic private database details")

    imports.uow_factory = lambda: LostResponse(store)
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    result = imports.confirm(preview.id, "a")
    assert result.status == "already_registered"
    assert result.receipt_id == preview.registration_id
    assert len(store.asset_units) == 27


def test_failed_commit_is_retryable_with_stable_identity(setup):
    store, imports, _ = setup

    class FailCommit(FakeUnitOfWork):
        def commit(self):
            raise RuntimeError("synthetic private database details")

    imports.uow_factory = lambda: FailCommit(store)
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    failed = imports.confirm(preview.id, "a")
    assert failed.status == "retryable"
    assert "private" not in str(failed)
    assert not store.receipts and not store.asset_units
    imports.uow_factory = lambda: FakeUnitOfWork(store)
    result = imports.confirm(preview.id, "a")
    assert result.receipt_id == preview.registration_id


def test_same_registration_uuid_with_conflicting_original_facts_is_rejected(setup, candidate, catalog):
    store, imports, _ = setup
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    command = validate_import_candidate(replace(candidate, source="OTHER"), catalog).command
    ids = iter([preview.registration_id, *(uuid4() for _ in range(28))])
    register_inventory_receipt(command, FakeUnitOfWork(store), new_id=lambda: next(ids))
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert len(store.receipts) == 1


def test_uncertain_commit_and_unavailable_reconciliation_can_recover_later(setup):
    store, imports, _ = setup
    unavailable = [False]
    original_inspect = imports.reader.inspect
    writes = []

    def inspect(*args):
        if unavailable[0]:
            raise RuntimeError("synthetic unavailable database")
        return original_inspect(*args)

    class LostResponse(FakeUnitOfWork):
        def commit(self):
            super().commit()
            writes.append(True)
            unavailable[0] = True
            raise RuntimeError("synthetic lost response")

    imports.reader.inspect = inspect
    imports.uow_factory = lambda: LostResponse(store)
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    assert imports.confirm(preview.id, "a").status == "retryable"
    assert len(store.asset_units) == 27
    unavailable[0] = False
    result = imports.confirm(preview.id, "a")
    assert result.status == "already_registered" and result.receipt_id == preview.registration_id
    assert len(writes) == 1


def test_timer_releases_candidate_without_another_request():
    previews = MemoryPreviewStore(ttl_seconds=0.05)
    parsed = parse_receipt_upload(csv_file(), "csv")
    released = Event()
    candidate_ref = ref(parsed.candidate, lambda _: released.set())
    preview = previews.create(parsed, "a")
    del parsed, preview
    assert released.wait(3)
    assert candidate_ref() is None
    previews.close()


def test_staging_memory_budget_rejects_an_upload_without_eviction():
    previews = MemoryPreviewStore(max_bytes=1024, start_timers=False)
    with pytest.raises(PreviewBusy):
        previews.create(parse_receipt_upload(csv_file(), "csv"), "a")
