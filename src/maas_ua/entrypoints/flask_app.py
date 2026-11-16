"""Development-only factory. No schema changes or connections during assembly."""

import logging
import os
import secrets
from hmac import compare_digest

from flask import Flask, abort, render_template, request, session
from werkzeug.exceptions import HTTPException, SecurityError

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.adapters.receipt_import_files import MAX_FILE_BYTES
from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.adapters.receipt_import_reader import SqlAlchemyReceiptImportReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.adapters.transfer_import_memory import MemoryTransferPreviewStore
from maas_ua.adapters.transfer_import_reader import SqlAlchemyTransferImportReader
from maas_ua.bootstrap import make_session_factory
from maas_ua.entrypoints.imports import blueprint
from maas_ua.entrypoints.inventory import blueprint as inventory_blueprint
from maas_ua.entrypoints.transfers import blueprint as transfers_blueprint
from maas_ua.service_layer.receipt_imports import (
    PreviewBusy,
    PreviewConsumed,
    PreviewUnavailable,
    ReceiptImportCoordinator,
)
from maas_ua.service_layer.transfer_imports import TransferImportCoordinator


class _PrivateAccessLog(logging.Filter):
    """The local Werkzeug server must not log URLs containing inventory identifiers."""

    def filter(self, record):
        if isinstance(record.msg, str) and '"%s" %s %s' in record.msg:
            # Werkzeug supplies request line, status, size. Keep only fixed text
            # plus status/size; even invalid request-line fallback content is private.
            if isinstance(record.args, tuple) and len(record.args) == 3:
                record.args = ("local request", record.args[1], record.args[2])
        return True


def create_app(config=None, *, imports=None, inventory=None, transfers=None):
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise ValueError("in-memory imports require a single local application process")
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=secrets.token_hex(32),
        DEBUG=False,
        MAX_CONTENT_LENGTH=MAX_FILE_BYTES + 128 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=8,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
    )
    if config:
        app.config.update(config)
    access_logger = logging.getLogger("werkzeug")
    if not any(isinstance(item, _PrivateAccessLog) for item in access_logger.filters):
        access_logger.addFilter(_PrivateAccessLog())
    factory = None
    if imports is None:
        factory = make_session_factory(app.config.get("DATABASE_URL"))
        imports = ReceiptImportCoordinator(
            SqlAlchemyReceiptImportReader(factory), lambda: SqlAlchemyUnitOfWork(factory), MemoryPreviewStore()
        )
        if inventory is None:
            inventory = SqlAlchemyInventoryReader(factory.kw["bind"])
    if transfers is None and factory is not None:
        transfers = TransferImportCoordinator(
            SqlAlchemyTransferImportReader(factory), lambda: SqlAlchemyUnitOfWork(factory), MemoryTransferPreviewStore()
        )
    app.extensions["transfer_imports"] = transfers
    app.extensions["receipt_imports"] = imports
    app.extensions["inventory_reader"] = inventory
    app.register_blueprint(blueprint)
    app.register_blueprint(inventory_blueprint)
    app.register_blueprint(transfers_blueprint)

    @app.before_request
    def protect_local_forms():
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403)
        if app.debug or request.environ.get("wsgi.multiprocess"):
            abort(503)
        if request.method == "POST":
            expected = session.get("csrf_token")
            supplied = request.form.get("csrf_token", "")
            if not expected or not supplied.isascii() or not compare_digest(expected, supplied):
                abort(400)
        session.setdefault("import_session", secrets.token_urlsafe(32))
        session.setdefault("csrf_token", secrets.token_urlsafe(32))

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(PreviewUnavailable)
    def missing_preview(error):
        return render_template(
            "imports/error.html", message="Preview unavailable or expired. Upload the file again."
        ), 410

    @app.errorhandler(PreviewBusy)
    @app.errorhandler(PreviewConsumed)
    def preview_conflict(error):
        return render_template("imports/error.html", message=str(error)), 409

    @app.errorhandler(HTTPException)
    def safe_http_error(error):
        if isinstance(error, SecurityError):
            return "Invalid request host.", 400
        messages = {
            400: "Invalid request, host, import type or CSRF token. Reload the upload page.",
            403: "This interface accepts local development requests only.",
            404: "Page not found.",
            413: "Upload at most 5 MiB.",
            415: "Only CSV and XLSX files are supported.",
            503: "Use one local application process with debug mode disabled.",
        }
        return render_template(
            "imports/error.html", message=messages.get(error.code, "The request could not be completed.")
        ), error.code

    return app
