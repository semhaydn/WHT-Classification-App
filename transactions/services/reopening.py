from django.contrib.auth.models import User
from django.db import transaction

from transactions.models import AuditEvent, LineExceptionRecord, OpenItem, ReviewDocumentSnapshot
from transactions.permissions import has_role
from transactions.services.gl_eligibility import is_open_item_classifiable


def _require_administrator(user: User) -> None:
    if not has_role(user, "Administrator"):
        raise PermissionError("Only Administrators can reopen completed invoices or lines.")


def _required_lines(lines: list[OpenItem]) -> list[OpenItem]:
    excluded_ids = set(
        LineExceptionRecord.objects.filter(
            open_item_id__in=[line.pk for line in lines],
            status=LineExceptionRecord.Status.EXCLUDED,
        ).values_list("open_item_id", flat=True)
    )
    return [
        line
        for line in lines
        if is_open_item_classifiable(line, excluded_item_ids=excluded_ids)
    ]


def _lock_completed_document(
    open_item: OpenItem,
) -> tuple[ReviewDocumentSnapshot, list[OpenItem], list[OpenItem]]:
    lines = list(
        OpenItem.objects.select_for_update()
        .filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
        .order_by("line_number")
    )
    snapshot = (
        ReviewDocumentSnapshot.objects.select_for_update()
        .filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
            is_current=True,
        )
        .first()
    )
    if snapshot is None:
        raise ValueError("This invoice is already active and does not need reopening.")
    if snapshot.review_status != ReviewDocumentSnapshot.ReviewStatus.REVIEWED:
        raise ValueError("Only a completed invoice can be reopened.")
    required_lines = _required_lines(lines)
    if not required_lines:
        raise ValueError("The invoice has no required accounting lines to reopen.")
    return snapshot, lines, required_lines


def _make_snapshot_historical(snapshot: ReviewDocumentSnapshot) -> None:
    snapshot.is_current = False
    snapshot.save(update_fields=["is_current"])


@transaction.atomic
def reopen_line(open_item: OpenItem, user: User) -> OpenItem:
    _require_administrator(user)
    item = OpenItem.objects.select_for_update().get(pk=open_item.pk)
    snapshot, _, required_lines = _lock_completed_document(item)
    if item.pk not in {line.pk for line in required_lines}:
        raise ValueError("Only a required, classifiable line can be reopened.")

    previous_status = item.status
    _make_snapshot_historical(snapshot)
    item.status = OpenItem.Status.IN_CLASSIFICATION
    item.save(update_fields=["status", "updated_at"])
    AuditEvent.objects.create(
        open_item=item,
        event_type=AuditEvent.EventType.LINE_REOPENED,
        performed_by=user,
        previous_value={
            "status": previous_status,
            "snapshot_id": snapshot.pk,
            "snapshot_version": snapshot.calculation_version,
        },
        new_value={
            "scope": "LINE",
            "entity_code": item.entity_code,
            "accounting_document_number": item.accounting_document_number,
            "line_id": item.pk,
            "line_number": item.line_number,
            "status": item.status,
        },
        comment="Administrator reopened this accounting line.",
    )
    return item


@transaction.atomic
def reopen_invoice(open_item: OpenItem, user: User) -> list[OpenItem]:
    _require_administrator(user)
    item = OpenItem.objects.select_for_update().get(pk=open_item.pk)
    snapshot, _, required_lines = _lock_completed_document(item)

    previous_statuses = [
        {
            "line_id": line.pk,
            "line_number": line.line_number,
            "status": line.status,
        }
        for line in required_lines
    ]
    _make_snapshot_historical(snapshot)
    for line in required_lines:
        line.status = OpenItem.Status.IN_CLASSIFICATION
        line.save(update_fields=["status", "updated_at"])
    AuditEvent.objects.create(
        open_item=required_lines[0],
        event_type=AuditEvent.EventType.INVOICE_REOPENED,
        performed_by=user,
        previous_value={
            "entity_code": item.entity_code,
            "accounting_document_number": item.accounting_document_number,
            "snapshot_id": snapshot.pk,
            "snapshot_version": snapshot.calculation_version,
            "line_statuses": previous_statuses,
        },
        new_value={
            "scope": "INVOICE",
            "entity_code": item.entity_code,
            "accounting_document_number": item.accounting_document_number,
            "reopened_line_ids": [line.pk for line in required_lines],
            "reopened_line_numbers": [line.line_number for line in required_lines],
            "status": OpenItem.Status.IN_CLASSIFICATION,
        },
        comment="Administrator reopened the invoice for re-review.",
    )
    return required_lines
