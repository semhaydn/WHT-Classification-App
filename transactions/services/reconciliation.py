from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping, Sequence

from transactions.models import AuditEvent, OpenItem, WHTDetermination


ZERO = Decimal("0.00")
CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")


def reconciliation_status_label(status: str) -> str:
    return {
        "PENDING": "Pending / Provisional",
        "RECONCILED": "Reconciled",
        "DISCREPANCY": "Discrepancy",
    }.get(status, status)


@dataclass(frozen=True)
class ReconciliationResult:
    calculated_wht: Decimal | None
    posted_wht: Decimal | None
    difference: Decimal | None
    status: str


def reconcile_amounts(
    calculated_wht: Decimal | None,
    posted_wht: Decimal | None,
) -> tuple[Decimal | None, str]:
    difference = (
        round_money(calculated_wht + posted_wht)
        if calculated_wht is not None and posted_wht is not None
        else None
    )
    if difference is None:
        return None, "PENDING"
    if abs(difference) <= TOLERANCE:
        return difference, "RECONCILED"
    return difference, "DISCREPANCY"


def round_money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def has_valid_determination(
    determination: WHTDetermination | None,
    *,
    expected_cost_type: str | None = None,
) -> bool:
    if determination is None or determination.wht_amount_snapshot is None:
        return False
    if (
        expected_cost_type is not None
        and determination.final_cost_type_snapshot != expected_cost_type
    ):
        return False
    if determination.rate_source == "MANUAL":
        return (
            determination.status == WHTDetermination.Status.ADMIN_RESOLVED
            and _valid_admin_override(determination)
            and determination.wht_rate_snapshot is not None
            and determination.wht_rate_snapshot.is_finite()
            and determination.wht_rate_snapshot > 0
        )
    if determination.rate_source == "ADMIN_NO_WHT":
        return (
            determination.status == WHTDetermination.Status.NO_WHT
            and _valid_admin_override(determination)
            and determination.wht_rate_snapshot == ZERO
            and determination.wht_amount_snapshot == ZERO
        )
    if determination.status == WHTDetermination.Status.MATCHED:
        return True
    if determination.status == WHTDetermination.Status.NO_WHT:
        return True
    return False


def _valid_admin_override(determination: WHTDetermination) -> bool:
    treatment = (
        "NO_WHT"
        if determination.rate_source == "ADMIN_NO_WHT"
        else "MANUAL_RATE"
    )
    if not (
        determination.manual_override_reason.strip()
        and determination.calculated_at
        and determination.calculated_by_id
        and determination.supplier_snapshot
        and determination.supplier_country_snapshot
        and determination.wht_rate_snapshot is not None
        and determination.wht_rate_snapshot.is_finite()
        and determination.wht_rate_snapshot >= ZERO
        and determination.wht_base_snapshot is not None
        and determination.wht_base_snapshot.is_finite()
    ):
        return False

    rate = determination.wht_rate_snapshot
    amount = determination.wht_amount_snapshot
    expected_amount = round_money(determination.wht_base_snapshot * rate)
    if amount != expected_amount:
        return False

    events = AuditEvent.objects.filter(
        open_item_id=determination.open_item_id,
        event_type=AuditEvent.EventType.WHT_EXCEPTION_RESOLVED,
        performed_by_id=determination.calculated_by_id,
        created_at__gte=determination.calculated_at,
        comment=determination.manual_override_reason,
        new_value__determination=determination.pk,
        new_value__treatment=treatment,
    ).values_list("new_value", flat=True)
    for event_value in events:
        try:
            event_rate = Decimal(str(event_value["rate"]))
            event_amount = Decimal(str(event_value["amount"]))
        except (InvalidOperation, KeyError, TypeError):
            continue
        allowed_event_statuses = {determination.status}
        if determination.status == WHTDetermination.Status.ADMIN_RESOLVED:
            allowed_event_statuses.add(WHTDetermination.Status.REQUIRES_REVIEW)
        if (
            event_value.get("status") in allowed_event_statuses
            and event_rate == rate
            and event_amount == amount
        ):
            return True
    return False


def determination_status_label(determination: WHTDetermination) -> str:
    if has_valid_determination(determination):
        if determination.rate_source == "MANUAL":
            return "Resolved one-off override"
        if determination.rate_source == "ADMIN_NO_WHT":
            return "Resolved one-off No WHT"
        if determination.status == WHTDetermination.Status.NO_WHT:
            return "No WHT"
    return determination.get_status_display()


def reconcile_document(
    lines: Sequence[OpenItem],
    required_lines: Sequence[OpenItem],
    determinations: Mapping[int, WHTDetermination | None],
    document_currency: str,
    expected_cost_types: Mapping[int, str | None] | None = None,
) -> ReconciliationResult:
    expected_cost_types = expected_cost_types or {}
    line_amounts: list[Decimal] = []
    complete = bool(document_currency)
    for line in required_lines:
        determination = determinations.get(line.pk)
        if not has_valid_determination(
            determination,
            expected_cost_type=expected_cost_types.get(line.pk),
        ):
            complete = False
            break
        if (
            determination.currency_snapshot != document_currency
            or line.document_currency != document_currency
        ):
            complete = False
            break
        line_amounts.append(determination.wht_amount_snapshot)

    calculated_wht = (
        round_money(sum(line_amounts, ZERO))
        if complete
        else None
    )

    posted_lines = [line for line in lines if line.gl_account == "229220"]
    posted_currencies = {line.document_currency for line in posted_lines}
    if not posted_lines:
        posted_wht = ZERO
    elif (
        document_currency
        and posted_currencies == {document_currency}
    ):
        posted_wht = round_money(
            sum((line.document_currency_amount for line in posted_lines), ZERO)
        )
    else:
        posted_wht = None

    difference, status = reconcile_amounts(calculated_wht, posted_wht)
    return ReconciliationResult(
        calculated_wht=calculated_wht,
        posted_wht=posted_wht,
        difference=difference,
        status=status,
    )
