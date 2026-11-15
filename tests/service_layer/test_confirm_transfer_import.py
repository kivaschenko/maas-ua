from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Event
from uuid import uuid4

import pytest
from import_fixtures import csv_file as receipt_csv
from transfer_import_fixtures import csv_file, transfer_fields

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.adapters.transfer_import_files import parse_transfer_upload
from maas_ua.adapters.transfer_import_memory import FakeTransferImportReader, MemoryTransferPreviewStore
from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipt_imports import PreviewBusy, PreviewConsumed, ReceiptImportCoordinator
from maas_ua.service_layer.transfer_imports import TransferImportCoordinator
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

    imports = TransferImportCoordinator(
        FakeTransferImportReader(store), factory, MemoryTransferPreviewStore(start_timers=False)
    )
    return store, imports, uows


def stage(imports, **kwargs):
    return imports.preview(parse_transfer_upload(csv_file(**kwargs), "csv"), "a")


def test_preview_has_resolved_units_without_writes_and_confirm_uses_one_commit(setup):
    store, imports, uows = setup
    before = tuple(store.asset_units.values())
    preview = stage(imports)
    assert preview.ready and len(preview.resolved) == 12
    assert preview.resolved[0].nomenclature_name == "Example"
    assert not uows and not store.movements
    result = imports.confirm(preview.id, "a")
    assert result.status == "registered" and result.movement_id == preview.registration_id
    assert len(result.asset_ids) == 12 and len(store.movements) == 1
    assert uows[0].commit_count == 1
    assert sum(u.custody.location_ref == "EXAMPLE-LOCATION" for u in store.asset_units.values()) == 15
    for unit in before:
        after = store.asset_units[unit.id]
        assert (after.id, after.receipt_line_id, after.serial_number, after.initial_custody) == (
            unit.id,
            unit.receipt_line_id,
            unit.serial_number,
            unit.initial_custody,
        )
    with pytest.raises(PreviewConsumed):
        imports.confirm(preview.id, "a")
    assert len(uows) == 1


def test_duplicate_and_missing_serials_identify_every_row(setup):
    store, imports, uows = setup
    preview = stage(imports, serials=[("EXAMPLE-ISSUER", "EXAMPLE-0000")] * 2 + [("missing", "00001")])
    assert {i.cell.row for i in preview.issues if i.code == "duplicate_serial"} == {2, 3}
    assert {i.cell.row for i in preview.issues if i.code == "missing_serial"} == {4}
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.movements


@pytest.mark.parametrize(
    "changes",
    [
        {"source_location_ref": "OTHER"},
        {"destination_location_ref": "EXAMPLE-LOCATION", "destination_responsible_party_ref": "EXAMPLE-PARTY"},
        {"effective_date": "2026-01-01"},
    ],
)
def test_business_rules_reject_whole_batch(setup, changes):
    store, imports, uows = setup
    preview = stage(imports, fields=transfer_fields(**changes))
    assert len(preview.issues) == 12
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.movements


@pytest.mark.parametrize("change", ["unknown", "mixed", "written_off"])
def test_one_ineligible_unit_blocks_every_row(setup, change):
    store, imports, uows = setup
    unit = next(iter(store.asset_units.values()))
    if change == "written_off":
        write_off_assets(
            WriteOffAssets(
                write_off_id=uuid4(),
                document_number="EXAMPLE-WO",
                document_date=date(2026, 1, 20),
                authorization_reference="EXAMPLE-AUTH",
                authorization_date=date(2026, 1, 20),
                effective_date=date(2026, 1, 20),
                asset_ids=(unit.id,),
            ),
            FakeUnitOfWork(store),
        )
        assert store.asset_units[unit.id].lifecycle_state is LifecycleState.WRITTEN_OFF
    else:
        with FakeUnitOfWork(store) as uow:
            uow.asset_units.save(
                replace(unit, custody=None if change == "unknown" else CustodyAssignment("OTHER", "OTHER"))
            )
            uow.commit()
    preview = stage(imports)
    assert len(preview.issues) == 1 and preview.issues[0].cell.row == 2
    assert imports.confirm(preview.id, "a").status == "rejected"
    assert not uows and not store.movements


def test_preview_is_advisory_and_reupload_conflicts(setup):
    store, imports, uows = setup
    first, stale = stage(imports), stage(imports)
    imports.confirm(first.id, "a")
    assert imports.confirm(stale.id, "a").status == "rejected"
    assert not stage(imports).ready
    assert len(store.movements) == len(uows) == 1


def test_conflicting_uuid_checks_immutable_membership(setup):
    store, imports, _ = setup
    preview = stage(imports)
    candidate = preview.candidate
    ids = tuple(u.id for u in store.asset_units.values())
    transfer_assets(
        TransferAssets(
            movement_id=preview.registration_id,
            document_number=candidate.document_number,
            document_date=candidate.document_date,
            effective_date=candidate.effective_date,
            source=candidate.source,
            destination=candidate.destination,
            asset_ids=ids[12:24],
        ),
        FakeUnitOfWork(store),
    )
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "movement_conflict"
    assert len(store.movements) == 1


def test_lost_commit_reconciles_after_subsequent_movement(setup):
    store, imports, _ = setup

    class LostResponse(FakeUnitOfWork):
        def commit(self):
            super().commit()
            original = next(iter(store.movements.values()))
            transfer_assets(
                TransferAssets(
                    movement_id=uuid4(),
                    document_number="EXAMPLE-NEXT",
                    document_date=date(2026, 3, 1),
                    effective_date=date(2026, 3, 1),
                    source=original.destination,
                    destination=original.source,
                    asset_ids=tuple(line.asset_unit_id for line in original.lines),
                ),
                FakeUnitOfWork(store),
            )
            raise RuntimeError("synthetic private failure")

    imports.uow_factory = lambda: LostResponse(store)
    preview = stage(imports)
    result = imports.confirm(preview.id, "a")
    assert result.status == "already_registered" and result.movement_id == preview.registration_id
    assert len(store.movements) == 2


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
    assert failures[0].rollback_count == 1 and not store.movements
    imports.uow_factory = lambda: FakeUnitOfWork(store)
    assert imports.confirm(preview.id, "a").movement_id == preview.registration_id


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
    assert len(uows) == len(store.movements) == 1


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
    assert not store.movements and not uows


def test_full_resolved_snapshots_count_toward_staging_budget(setup):
    _, imports, _ = setup
    parsed = parse_transfer_upload(csv_file(), "csv")
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
    transfer_assets(
        TransferAssets(
            movement_id=preview.registration_id,
            document_number="OTHER-DOCUMENT",
            document_date=c.document_date,
            effective_date=c.effective_date,
            source=c.source,
            destination=c.destination,
            asset_ids=tuple(u.id for u in store.asset_units.values())[:12],
        ),
        FakeUnitOfWork(store),
    )
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "movement_conflict"
