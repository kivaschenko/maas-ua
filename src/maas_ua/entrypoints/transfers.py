"""Thin local transfer-import HTTP transport."""

import csv
from io import BytesIO, StringIO
from pathlib import PurePath

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, url_for
from openpyxl import Workbook

from maas_ua.adapters.receipt_import_files import MAX_FILE_BYTES, SERIAL_FIELDS
from maas_ua.adapters.transfer_import_files import DOCUMENT_FIELDS, parse_transfer_upload
from maas_ua.entrypoints.imports import _owner
from maas_ua.service_layer.receipt_imports import PreviewBusy, PreviewConsumed, PreviewUnavailable
from maas_ua.service_layer.transfer_imports import IMPORT_TYPE

blueprint = Blueprint("transfers", __name__, url_prefix="/transfers/import")


def _imports():
    imports = current_app.extensions.get("transfer_imports")
    if imports is None:
        abort(503)
    return imports


@blueprint.errorhandler(PreviewUnavailable)
def unavailable(error):
    return render_template(
        "transfers/error.html", message="Перегляд недоступний або термін дії минув. Завантажте файл знову."
    ), 410


@blueprint.errorhandler(PreviewBusy)
@blueprint.errorhandler(PreviewConsumed)
def busy(error):
    return render_template(
        "transfers/error.html", message="Перегляд зайнятий, уже підтверджений або сховище заповнене."
    ), 409


@blueprint.get("/new")
def new():
    return render_template("transfers/upload.html", import_type=IMPORT_TYPE)


@blueprint.post("")
def upload():
    if request.form.get("import_type") != IMPORT_TYPE:
        abort(400)
    files = request.files.getlist("file")
    if len(files) != 1 or len(request.files) != 1:
        abort(400)
    uploaded = files[0]
    kind = PurePath(uploaded.filename or "").suffix.lower().lstrip(".")
    try:
        if kind not in {"csv", "xlsx"}:
            abort(415)
        data = uploaded.stream.read(MAX_FILE_BYTES + 1)
    finally:
        uploaded.close()
    if len(data) > MAX_FILE_BYTES:
        abort(413)
    parsed = parse_transfer_upload(data, kind, delimiter=request.form.get("delimiter", ","))
    item = _imports().preview(parsed, _owner())
    return redirect(url_for("transfers.preview", preview_id=item.id), code=303)


@blueprint.get("/<uuid:preview_id>")
def preview(preview_id):
    item = _imports().previews.get(preview_id, _owner())
    if item.state == "consumed":
        return redirect(url_for("transfers.result", preview_id=preview_id), code=303)
    return render_template("transfers/preview.html", preview=item)


@blueprint.post("/<uuid:preview_id>/confirm")
def confirm(preview_id):
    try:
        outcome = _imports().confirm(preview_id, _owner())
    except PreviewConsumed:
        return render_template(
            "transfers/error.html",
            message="Цей перегляд уже підтверджено. Повторного переміщення не виконано.",
            result_url=url_for("transfers.result", preview_id=preview_id),
        ), 409
    if outcome.status == "retryable":
        return render_template("transfers/result.html", result=outcome, preview_id=preview_id), 503
    return redirect(url_for("transfers.result", preview_id=preview_id), code=303)


@blueprint.get("/<uuid:preview_id>/result")
def result(preview_id):
    item = _imports().previews.get(preview_id, _owner())
    if item.result is None:
        return redirect(url_for("transfers.preview", preview_id=preview_id), code=303)
    return render_template("transfers/result.html", result=item.result, preview_id=preview_id)


@blueprint.post("/<uuid:preview_id>/discard")
def discard(preview_id):
    _imports().previews.discard(preview_id, _owner())
    return redirect(url_for("transfers.new"), code=303)


@blueprint.get("/template/<file_type>")
def template(file_type):
    if file_type == "csv":
        stream = StringIO(newline="")
        csv.writer(stream).writerow([*DOCUMENT_FIELDS, *SERIAL_FIELDS])
        content, mime = stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    elif file_type == "xlsx":
        workbook = Workbook()
        document = workbook.active
        document.title = "Document"
        document.append(["field", "value"])
        for field in DOCUMENT_FIELDS:
            document.append([field, IMPORT_TYPE if field == "template_version" else ""])
            document.cell(document.max_row, 2).number_format = "@"
        document.column_dimensions["A"].width = 40
        document.column_dimensions["B"].width = 32
        document.freeze_panes = "B2"
        serials = workbook.create_sheet("SerialNumbers")
        serials.append(list(SERIAL_FIELDS))
        for column in ("A", "B"):
            serials.column_dimensions[column].width = 32
            serials.column_dimensions[column].number_format = "@"
        serials.freeze_panes = "A2"
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        content, mime = stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        abort(415)
    return send_file(BytesIO(content), mimetype=mime, as_attachment=True, download_name=f"{IMPORT_TYPE}.{file_type}")
