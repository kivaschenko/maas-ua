"""Read-only HTTP transport for the existing inventory query contracts."""

from datetime import date, datetime
from functools import wraps
from uuid import UUID
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from maas_ua.domain.custody import LifecycleState
from maas_ua.service_layer.inventory import (
    AssetNotFound,
    GetAssetBySerialNumber,
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryBalance,
    GetInventoryValuation,
    InvalidInventoryQuery,
    InventoryFilter,
    get_asset_by_serial_number,
    get_asset_history,
    get_asset_position_as_of,
    get_inventory_balance,
    get_inventory_valuation,
)

blueprint = Blueprint("inventory", __name__, url_prefix="/inventory")

DIAGNOSTICS = {
    "missing_provenance": "Немає повного підтвердженого походження майна.",
    "missing_initial_custody": "Не встановлено початкове місце зберігання або відповідальну особу.",
    "unknown_lifecycle": "Не встановлено обліковий стан майна.",
    "ambiguous_movement_order": "Невідомий порядок переміщень протягом одного дня.",
    "broken_custody_chain": "Послідовність передачі відповідальності має розрив.",
    "movement_before_receipt": "Переміщення датоване раніше за надходження.",
    "invalid_write_off_history": "Відомості про списання не узгоджені з історією майна.",
}


class InvalidInput(ValueError):
    """A fixed, user-facing HTTP diagnostic; never includes submitted values."""


def exact_text(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip() or "\x00" in value:
        raise InvalidInput("Введіть точне непорожнє значення без пробілів на початку чи в кінці.")
    return value


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except TypeError, ValueError:
        raise InvalidInput("Вкажіть дійсну дату у форматі РРРР-ММ-ДД.") from None


def parse_uuid(value):
    try:
        return UUID(value)
    except TypeError, ValueError, AttributeError:
        raise InvalidInput("Відсутній або неправильний ідентифікатор майна чи документа.") from None


def decimal_text(value):
    """Display exact decimal digits without rounding or currency quantization."""
    return format(value, "f")


def diagnostic_text(code):
    return DIAGNOSTICS.get(code, "Дані неповні; підтверджений результат недоступний.")


blueprint.add_app_template_filter(decimal_text, "inventory_decimal")
blueprint.add_app_template_filter(diagnostic_text, "inventory_diagnostic")


def _as_of():
    value = request.args.get("as_of")
    return parse_date(value) if value is not None else datetime.now(ZoneInfo("Europe/Kyiv")).date()


def _reader():
    reader = current_app.extensions.get("inventory_reader")
    if reader is None:
        raise RuntimeError("inventory reader is not configured")
    return reader


def _safe_view(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            if any(len(request.args.getlist(key)) != 1 for key in request.args):
                raise InvalidInput("Кожен параметр запиту можна вказати лише один раз.")
            return view(*args, **kwargs)
        except InvalidInput as error:
            return render_template("inventory/error.html", message=str(error)), 400
        except InvalidInventoryQuery:
            return render_template("inventory/error.html", message="Перевірте точні значення параметрів запиту."), 400
        except AssetNotFound:
            return render_template("inventory/error.html", message="Майно за вказаними даними не знайдено."), 404
        except Exception:
            # Do not interpolate or log identifiers, SQL parameters, or underlying exceptions.
            return render_template(
                "inventory/error.html", message="Не вдалося отримати перевірені дані. Спробуйте пізніше."
            ), 503

    return wrapped


@blueprint.get("/search")
@_safe_view
def search():
    if "namespace" in request.args or "serial_value" in request.args:
        query = GetAssetBySerialNumber(
            namespace=exact_text(request.args.get("namespace")), value=exact_text(request.args.get("serial_value"))
        )
        asset = get_asset_by_serial_number(query, _reader())
        return redirect(url_for("inventory.asset", asset_id=asset.asset_id), code=303)
    return render_template("inventory/search.html")


@blueprint.get("/assets/", defaults={"asset_id": ""})
@blueprint.get("/assets/<asset_id>")
@_safe_view
def asset(asset_id):
    identity, as_of = parse_uuid(asset_id), _as_of()
    reader = _reader()
    history = get_asset_history(GetAssetHistory(asset_id=identity), reader)
    # These independent read operations each own a snapshot. No cross-call
    # consistency is claimed. date.max means all recorded effective events.
    current = get_asset_position_as_of(GetAssetPositionAsOf(asset_id=identity, as_of=date.max), reader)
    position = get_asset_position_as_of(GetAssetPositionAsOf(asset_id=identity, as_of=as_of), reader)
    return render_template(
        "inventory/asset.html", history=history, asset=history.asset, current=current, position=position, as_of=as_of
    )


@blueprint.get("/balances")
@_safe_view
def balances():
    as_of = _as_of()
    filters = {}
    for key in ("nomenclature_id", "source", "location_ref", "responsible_party_ref", "currency"):
        if request.args.get(key, "") != "":
            filters[key] = exact_text(request.args[key])
    if request.args.get("receipt_id", "") != "":
        filters["receipt_id"] = parse_uuid(request.args["receipt_id"])
    state = request.args.get("lifecycle_state", "ACTIVE")
    try:
        filters["lifecycle_state"] = None if state == "ALL" else LifecycleState(state)
    except ValueError:
        raise InvalidInput("Оберіть обліковий стан зі списку.") from None
    view = request.args.get("view", "balance")
    if view not in {"balance", "valuation"}:
        raise InvalidInput("Оберіть вид звіту зі списку.")
    inputs = dict(as_of=as_of, filters=InventoryFilter(**filters))
    # A single set-based operation provides groups and currency totals in one snapshot.
    report = (
        get_inventory_balance(GetInventoryBalance(**inputs), _reader())
        if view == "balance"
        else get_inventory_valuation(GetInventoryValuation(**inputs), _reader())
    )
    return render_template("inventory/balances.html", report=report, as_of=as_of, view=view, state=state)
