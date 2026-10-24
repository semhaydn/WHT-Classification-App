from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from transactions.models import (
    AuditEvent,
    ClassificationDecision,
    LineExceptionRecord,
    OpenItem,
    ReviewDocumentSnapshot,
    WHTDetermination,
)
from transactions.permissions import has_role
from transactions.services.gl_eligibility import is_open_item_classifiable
from transactions.services.wht_determination import determine_wht
from transactions.services.reconciliation import has_valid_determination


@dataclass(frozen=True)
class ExceptionQueueEntry:
    open_item: OpenItem
    issue_type: str
    status: str
    reason: str
    created_at: object
    requested_by: User | None
    record: LineExceptionRecord | None = None
    determination: WHTDetermination | None = None
    completed: bool = False


def _require_administrator(user: User | None) -> None:
    if not has_role(user, "Administrator"):
        raise PermissionError("Only Administrators can resolve exceptions.")


def _require_active_line(open_item: OpenItem) -> None:
    if ReviewDocumentSnapshot.objects.filter(
        entity_code=open_item.entity_code,
        accounting_document_number=open_item.accounting_document_number,
        is_current=True,
    ).exists():
        raise ValueError("Completed invoices are immutable and cannot be changed.")


@transaction.atomic
def flag_line_for_review(
    open_item: OpenItem,
    user: User,
    reason: str,
) -> LineExceptionRecord:
    if not has_role(user, "Transaction User", "Administrator"):
        raise PermissionError("A Transaction User role is required to flag a line.")
    if not reason.strip():
        raise ValueError("A reason is required to flag a line.")
    _require_active_line(open_item)
    if LineExceptionRecord.objects.filter(
        open_item=open_item,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        status=LineExceptionRecord.Status.OPEN,
    ).exists():
        raise ValueError("This line already has an unresolved Administrator review flag.")
    record = LineExceptionRecord.objects.create(
        open_item=open_item,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        reason=reason.strip(),
        created_by=user,
    )
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=AuditEvent.EventType.ADMIN_REVIEW_FLAGGED,
        performed_by=user,
        new_value={
            "exception_record": record.pk,
            "issue_type": record.issue_type,
            "status": record.status,
        },
        comment=record.reason,
    )
    return record


@transaction.atomic
def resolve_line_flag(
    record: LineExceptionRecord,
    user: User,
    decision: str,
    *,
    exclude_line: bool = False,
) -> LineExceptionRecord:
    _require_administrator(user)
    if record.issue_type != LineExceptionRecord.Type.USER_FLAG:
        raise ValueError("Only a user review flag can be resolved with this action.")
    if record.status != LineExceptionRecord.Status.OPEN:
        raise ValueError("This Administrator review flag is no longer open.")
    if not decision.strip():
        raise ValueError("A recorded administrative decision is required.")
    _require_active_line(record.open_item)
    previous_status = record.status
    record.status = (
        LineExceptionRecord.Status.EXCLUDED
        if exclude_line
        else LineExceptionRecord.Status.RESOLVED
    )
    record.resolution_decision = decision.strip()
    record.resolved_by = user
    record.resolved_at = timezone.now()
    record.save(
        update_fields=[
            "status",
            "resolution_decision",
            "resolved_by",
            "resolved_at",
        ]
    )
    AuditEvent.objects.create(
        open_item=record.open_item,
        event_type=(
            AuditEvent.EventType.LINE_EXCLUDED
            if exclude_line
            else AuditEvent.EventType.ADMIN_REVIEW_CLEARED
        ),
        performed_by=user,
        previous_value={"exception_record": record.pk, "status": previous_status},
        new_value={"exception_record": record.pk, "status": record.status},
        comment=record.resolution_decision,
    )
    return record


@transaction.atomic
def exclude_line(
    open_item: OpenItem,
    user: User,
    reason: str,
) -> LineExceptionRecord:
    _require_administrator(user)
    if not reason.strip():
        raise ValueError("A mandatory exclusion reason is required.")
    _require_active_line(open_item)
    existing = LineExceptionRecord.objects.filter(
        open_item=open_item,
        status=LineExceptionRecord.Status.EXCLUDED,
    ).first()
    if existing is not None:
        raise ValueError("This accounting line is already explicitly excluded.")
    unresolved_flag = LineExceptionRecord.objects.filter(
        open_item=open_item,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        status=LineExceptionRecord.Status.OPEN,
    ).first()
    if unresolved_flag is not None:
        unresolved_flag.status = LineExceptionRecord.Status.EXCLUDED
        unresolved_flag.resolution_decision = reason.strip()
        unresolved_flag.resolved_by = user
        unresolved_flag.resolved_at = timezone.now()
        unresolved_flag.save(
            update_fields=[
                "status",
                "resolution_decision",
                "resolved_by",
                "resolved_at",
            ]
        )
        AuditEvent.objects.create(
            open_item=open_item,
            event_type=AuditEvent.EventType.LINE_EXCLUDED,
            performed_by=user,
            previous_value={
                "exception_record": unresolved_flag.pk,
                "status": LineExceptionRecord.Status.OPEN,
            },
            new_value={
                "exception_record": unresolved_flag.pk,
                "status": LineExceptionRecord.Status.EXCLUDED,
            },
            comment=reason.strip(),
        )
        return unresolved_flag

    record = LineExceptionRecord.objects.create(
        open_item=open_item,
        issue_type=LineExceptionRecord.Type.ADMIN_EXCLUSION,
        status=LineExceptionRecord.Status.EXCLUDED,
        reason=reason.strip(),
        created_by=user,
        resolution_decision=reason.strip(),
        resolved_by=user,
        resolved_at=timezone.now(),
    )
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=AuditEvent.EventType.LINE_EXCLUDED,
        performed_by=user,
        new_value={
            "exception_record": record.pk,
            "issue_type": record.issue_type,
            "status": record.status,
        },
        comment=record.reason,
    )
    return record


def document_wht_issues(
    entity_code: str,
    accounting_document_number: str,
) -> list[ExceptionQueueEntry]:
    lines = list(
        OpenItem.objects.filter(
            entity_code=entity_code,
            accounting_document_number=accounting_document_number,
        ).select_related("source_import_batch")
    )
    entries = []
    for line in lines:
        determination = line.wht_determinations.order_by(
            "-calculated_at", "-id"
        ).first()
        decision = line.classification_decisions.filter(is_current=True).first()
        if not is_open_item_classifiable(line) or decision is None:
            continue
        if determination is None:
            entries.append(
                ExceptionQueueEntry(
                    line,
                    "WHT MISSING",
                    "Unresolved",
                    "No WHT determination exists for the current Final Cost Type.",
                    line.updated_at,
                    None,
                    completed=ReviewDocumentSnapshot.objects.filter(
                        entity_code=line.entity_code,
                        accounting_document_number=line.accounting_document_number,
                        is_current=True,
                    ).exists(),
                )
            )
            continue
        if determination.final_cost_type_snapshot != decision.final_cost_type.code:
            entries.append(
                ExceptionQueueEntry(
                    line,
                    "WHT STALE",
                    "Unresolved",
                    "The determination uses a different Final Cost Type.",
                    determination.calculated_at,
                    determination.calculated_by,
                    determination=determination,
                    completed=ReviewDocumentSnapshot.objects.filter(
                        entity_code=line.entity_code,
                        accounting_document_number=line.accounting_document_number,
                        is_current=True,
                    ).exists(),
                )
            )
            continue
        if not has_valid_determination(
            determination,
            expected_cost_type=decision.final_cost_type.code,
        ):
            explanation = (
                "Supplier identity or Supplier Country is missing or ambiguous."
                if not determination.supplier_snapshot
                or not determination.supplier_country_snapshot
                else (
                    "WHT determination status is "
                    f"{determination.get_status_display()}."
                )
            )
            entries.append(
                ExceptionQueueEntry(
                    line,
                    determination.get_status_display().upper(),
                    "Unresolved",
                    explanation,
                    determination.calculated_at,
                    determination.calculated_by,
                    determination=determination,
                    completed=ReviewDocumentSnapshot.objects.filter(
                        entity_code=line.entity_code,
                        accounting_document_number=line.accounting_document_number,
                        is_current=True,
                    ).exists(),
                )
            )
    return entries


def build_exception_queue() -> list[ExceptionQueueEntry]:
    records = LineExceptionRecord.objects.filter(
        status__in=[
            LineExceptionRecord.Status.OPEN,
            LineExceptionRecord.Status.EXCLUDED,
        ]
    ).select_related("open_item", "created_by", "resolved_by")
    entries = [
        ExceptionQueueEntry(
            record.open_item,
            record.get_issue_type_display(),
            record.get_status_display(),
            record.reason,
            record.created_at,
            record.created_by,
            record=record,
            completed=ReviewDocumentSnapshot.objects.filter(
                entity_code=record.open_item.entity_code,
                accounting_document_number=record.open_item.accounting_document_number,
                is_current=True,
            ).exists(),
        )
        for record in records
    ]
    documents = (
        OpenItem.objects.values_list(
            "entity_code",
            "accounting_document_number",
        )
        .distinct()
    )
    for entity_code, document_number in documents:
        entries.extend(document_wht_issues(entity_code, document_number))
    return sorted(entries, key=lambda entry: entry.created_at, reverse=True)


@transaction.atomic
def resolve_wht_exception(
    open_item: OpenItem,
    user: User,
    reason: str,
    *,
    manual_rate=None,
    no_wht: bool = False,
) -> WHTDetermination:
    _require_administrator(user)
    if not reason.strip():
        raise ValueError("A mandatory resolution reason is required.")
    reason = reason.strip()
    if manual_rate is None and not no_wht:
        raise ValueError("Choose a manual rate or an explicit No WHT decision.")
    if not is_open_item_classifiable(open_item):
        raise ValueError("Only active, non-zero eligible lines can be resolved.")
    _require_active_line(open_item)
    decision = open_item.classification_decisions.filter(is_current=True).first()
    if decision is None:
        raise ValueError("A current Final Cost Type is required.")
    previous = open_item.wht_determinations.order_by(
        "-calculated_at", "-id"
    ).first()
    if previous is not None and has_valid_determination(
        previous,
        expected_cost_type=decision.final_cost_type.code,
    ) and not LineExceptionRecord.objects.filter(
        open_item=open_item,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        status=LineExceptionRecord.Status.OPEN,
    ).exists():
        raise ValueError("This line has no unresolved WHT determination exception.")
    determination = determine_wht(
        open_item,
        decision,
        manual_rate=manual_rate,
        manual_reason=reason,
        user=user,
        manual_no_wht=no_wht,
    )
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=AuditEvent.EventType.WHT_EXCEPTION_RESOLVED,
        performed_by=user,
        previous_value=(
            {
                "determination": previous.pk,
                "status": previous.status,
                "rate": str(previous.wht_rate_snapshot),
                "amount": str(previous.wht_amount_snapshot),
            }
            if previous is not None
            else None
        ),
        new_value={
            "determination": determination.pk,
            "status": determination.status,
            "rate": str(determination.wht_rate_snapshot),
            "amount": str(determination.wht_amount_snapshot),
            "treatment": "NO_WHT" if no_wht else "MANUAL_RATE",
        },
        comment=reason.strip(),
    )
    return determination


@transaction.atomic
def recalculate_wht_exception(
    open_item: OpenItem,
    user: User,
) -> WHTDetermination:
    _require_administrator(user)
    if not is_open_item_classifiable(open_item):
        raise ValueError("Only active, non-zero eligible lines can be recalculated.")
    _require_active_line(open_item)
    decision = open_item.classification_decisions.filter(is_current=True).first()
    if decision is None:
        raise ValueError("A current Final Cost Type is required.")
    previous = open_item.wht_determinations.order_by(
        "-calculated_at", "-id"
    ).first()
    determination = determine_wht(open_item, decision, user=user)
    previous_workflow_status = open_item.status
    if open_item.status in (
        OpenItem.Status.READY_FOR_REVIEW,
        OpenItem.Status.UNDER_REVIEW,
        OpenItem.Status.REVIEWED,
    ):
        open_item.status = OpenItem.Status.COMPLETED
        open_item.save(update_fields=["status", "updated_at"])
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=AuditEvent.EventType.WHT_DETERMINATION_RECALCULATED,
        performed_by=user,
        new_value={
            "determination": determination.pk,
            "status": determination.status,
            "calculation_version": determination.calculation_version,
            "workflow_status": open_item.status,
        },
        previous_value={
            **(
                {
                    "determination": previous.pk,
                    "status": previous.status,
                    "calculation_version": previous.calculation_version,
                }
                if previous is not None
                else {}
            ),
            "workflow_status": previous_workflow_status,
        },
        comment=(
            "Administrator explicitly recalculated the active determination. "
            "A line awaiting review was returned to Completed and must enter a new review."
            if previous_workflow_status
            in (
                OpenItem.Status.READY_FOR_REVIEW,
                OpenItem.Status.UNDER_REVIEW,
                OpenItem.Status.REVIEWED,
            )
            else "Administrator explicitly recalculated the active determination."
        ),
    )
    return determination


def document_completion_blockers(open_item: OpenItem) -> list[str]:
    lines = list(
        OpenItem.objects.filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
    )
    required_lines = [line for line in lines if is_open_item_classifiable(line)]
    blockers: list[str] = []
    for line in required_lines:
        decision = line.classification_decisions.filter(is_current=True).first()
        if decision is None:
            blockers.append(f"Line {line.line_number}: Final Cost Type is missing.")
            continue
        determination = line.wht_determinations.order_by(
            "-calculated_at", "-id"
        ).first()
        if determination is None:
            blockers.append(f"Line {line.line_number}: WHT determination is missing.")
        elif determination.final_cost_type_snapshot != decision.final_cost_type.code:
            blockers.append(f"Line {line.line_number}: WHT determination is stale.")
        elif not has_valid_determination(
            determination,
            expected_cost_type=decision.final_cost_type.code,
        ):
            blockers.append(
                f"Line {line.line_number}: WHT determination is unresolved."
            )
        if line.status not in (
            OpenItem.Status.COMPLETED,
            OpenItem.Status.READY_FOR_REVIEW,
            OpenItem.Status.UNDER_REVIEW,
            OpenItem.Status.REVIEWED,
        ):
            blockers.append(f"Line {line.line_number}: classification is unfinished.")
    if LineExceptionRecord.objects.filter(
        open_item__in=required_lines,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        status=LineExceptionRecord.Status.OPEN,
    ).exists():
        blockers.append("An unresolved Administrator review flag remains.")
    return blockers
