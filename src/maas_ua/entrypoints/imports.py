"""Form transport only: receipt decisions stay in application/domain services."""

import csv
from io import BytesIO, StringIO
from pathlib import PurePath

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook

from maas_ua.adapters.receipt_import_files import DOCUMENT_FIELDS, MAX_FILE_BYTES, SERIAL_FIELDS, parse_receipt_upload
from maas_ua.service_layer.receipt_imports import IMPORT_TYPE, PreviewConsumed

blueprint = Blueprint("imports", __name__)


def _imports():
    return current_app.extensions["receipt_imports"]


def _owner():
    return session["import_session"]


@blueprint.get("/")
def index():
    return redirect(url_for("imports.new"), code=303)


@blueprint.get("/imports/new")
def new():
    return render_template("imports/upload.html", import_type=IMPORT_TYPE)


@blueprint.post("/imports")
def upload():
    if request.form.get("import_type") != IMPORT_TYPE:
        abort(400)
    files = request.files.getlist("file")
    if len(files) != 1 or len(request.files) != 1:
        abort(400)
    uploaded = files[0]
    file_type = PurePath(uploaded.filename or "").suffix.lower().lstrip(".")
    if file_type not in {"csv", "xlsx"}:
        uploaded.close()
        abort(415)
    try:
        data = uploaded.stream.read(MAX_FILE_BYTES + 1)
    finally:
        uploaded.close()
    if len(data) > MAX_FILE_BYTES:
        abort(413)
    parsed = parse_receipt_upload(data, file_type, delimiter=request.form.get("delimiter", ","))
    preview = _imports().preview(parsed, _owner())
    return redirect(url_for("imports.preview", preview_id=preview.id), code=303)


@blueprint.get("/imports/<uuid:preview_id>")
def preview(preview_id):
    item = _imports().previews.get(preview_id, _owner())
    if item.state == "consumed":
        return redirect(url_for("imports.result", preview_id=preview_id), code=303)
    return render_template("imports/preview.html", preview=item)


@blueprint.post("/imports/<uuid:preview_id>/confirm")
def confirm(preview_id):
    try:
        outcome = _imports().confirm(preview_id, _owner())
    except PreviewConsumed:
        return render_template(
            "imports/error.html",
            message="This preview was already consumed. No additional registration was performed.",
            result_url=url_for("imports.result", preview_id=preview_id),
        ), 409
    if outcome.status == "retryable":
        return render_template("imports/result.html", result=outcome, receipt=None, preview_id=preview_id), 503
    return redirect(url_for("imports.result", preview_id=preview_id), code=303)


@blueprint.get("/imports/<uuid:preview_id>/result")
def result(preview_id):
    item = _imports().previews.get(preview_id, _owner())
    if item.result is None:
        return redirect(url_for("imports.preview", preview_id=preview_id), code=303)
    try:
        receipt = _imports().receipt(preview_id, _owner())
    except Exception:
        return render_template(
            "imports/error.html", message="The registered document cannot be retrieved right now. Retry this page."
        ), 503
    return render_template("imports/result.html", result=item.result, receipt=receipt, preview_id=preview_id)


@blueprint.post("/imports/<uuid:preview_id>/discard")
def discard(preview_id):
    _imports().previews.discard(preview_id, _owner())
    return redirect(url_for("imports.new"), code=303)


@blueprint.get("/imports/template/<file_type>")
def template(file_type):
    values = {field: "" for field in DOCUMENT_FIELDS}
    values["template_version"], values["accounting_classification"] = IMPORT_TYPE, "INVENTORY"
    if file_type == "csv":
        stream = StringIO(newline="")
        csv.writer(stream).writerow([*DOCUMENT_FIELDS, "line_key", *SERIAL_FIELDS, "analytic.example"])
        content, mime = stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    elif file_type == "xlsx":
        workbook = Workbook()
        document = workbook.active
        document.title = "Document"
        document.append(["field", "value"])
        for field, value in values.items():
            document.append([field, value])
            if field not in {"unit_price", "line_total", "line_quantity", "document_date", "effective_date"}:
                document.cell(document.max_row, 2).number_format = "@"
        document.column_dimensions["A"].width = 34
        document.column_dimensions["B"].width = 32
        document.freeze_panes = "B2"
        serials = workbook.create_sheet("SerialNumbers")
        serials.append(list(SERIAL_FIELDS))
        for name in ("A", "B"):
            serials.column_dimensions[name].width = 32
            serials.column_dimensions[name].number_format = "@"
        serials.freeze_panes = "A2"
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        content, mime = stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        abort(415)
    return send_file(BytesIO(content), mimetype=mime, as_attachment=True, download_name=f"{IMPORT_TYPE}.{file_type}")
