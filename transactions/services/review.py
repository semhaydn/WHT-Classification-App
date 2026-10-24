from django.contrib.auth.models import User
from django.db import transaction

from transactions.models import AuditEvent, OpenItem, ReviewAction
from transactions.models import ReviewDocumentSnapshot
from transactions.services.gl_eligibility import is_open_item_classifiable
from transactions.services.exceptions import document_completion_blockers
from transactions.services.reconciliation import has_valid_determination
from transactions.permissions import has_role
from .classification import save_final_classification
from .review_snapshots import create_review_document_snapshot


def _record_action(
    open_item: OpenItem,
    action_type: str,
    user: User | None,
    comments: str = "",
    override_reason: str = "",
) -> ReviewAction:
    action = ReviewAction.objects.create(
        open_item=open_item,
        action_type=action_type,
        performed_by=user,
        comments=comments,
        override_reason=override_reason,
    )
    event_type = {
        ReviewAction.ActionType.REVIEW_REQUESTED: AuditEvent.EventType.SUBMITTED_FOR_REVIEW,
        ReviewAction.ActionType.SUBMITTED: AuditEvent.EventType.SUBMITTED_FOR_REVIEW,
        ReviewAction.ActionType.REVIEW_STARTED: AuditEvent.EventType.REVIEW_STARTED,
        ReviewAction.ActionType.REVIEW_COMPLETED: AuditEvent.EventType.REVIEW_COMPLETED,
        ReviewAction.ActionType.APPROVED: AuditEvent.EventType.APPROVED,
        ReviewAction.ActionType.RETURNED: AuditEvent.EventType.RETURNED,
    }[action_type]
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=event_type,
        performed_by=user,
        new_value={"status": open_item.status},
        comment=comments or override_reason,
    )
    return action


def _require_classifiable(open_item: OpenItem) -> None:
    if not is_open_item_classifiable(open_item):
        raise ValueError("Only non-zero postings on eligible G/L accounts can enter review.")


@transaction.atomic
def finalize_invoice(open_item: OpenItem, user: User) -> ReviewDocumentSnapshot:
    if not has_role(user, "Transaction User", "Administrator"):
        raise PermissionError("A Transaction User role is required to finalize invoices.")
    lines = list(
        OpenItem.objects.select_for_update()
        .filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
        .order_by("line_number", "pk")
    )
    if not lines:
        raise ValueError("The invoice has no accounting lines to finalize.")
    anchor = lines[0]
    current = ReviewDocumentSnapshot.objects.filter(
        entity_code=anchor.entity_code,
        accounting_document_number=anchor.accounting_document_number,
        is_current=True,
    ).first()
    if current is not None:
        raise ValueError("This invoice is already finalized.")

    required_lines = [line for line in lines if is_open_item_classifiable(line)]
    if not required_lines:
        raise ValueError("The invoice has no non-zero eligible lines to finalize.")
    blockers = document_completion_blockers(anchor)
    if blockers:
        raise ValueError("Invoice finalization is blocked: " + " ".join(blockers))
    unfinished = [
        line for line in required_lines
        if line.status not in (OpenItem.Status.COMPLETED, OpenItem.Status.REVIEWED)
    ]
    if unfinished:
        raise ValueError(
            "Invoice finalization is blocked: "
            + " ".join(
                f"Line {line.line_number}: classification is unfinished."
                for line in unfinished
            )
        )

    for line in required_lines:
        if line.status != OpenItem.Status.REVIEWED:
            line.status = OpenItem.Status.REVIEWED
            line.save(update_fields=["status", "updated_at"])

    completion_item = required_lines[0]
    action = ReviewAction.objects.create(
        open_item=completion_item,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
        performed_by=user,
        comments="Invoice finalized.",
    )
    snapshot = create_review_document_snapshot(
        completion_item,
        completion_action=action,
    )
    AuditEvent.objects.create(
        open_item=completion_item,
        event_type=AuditEvent.EventType.INVOICE_FINALIZED,
        performed_by=user,
        new_value={
            "snapshot_id": snapshot.pk,
            "snapshot_version": snapshot.calculation_version,
            "accounting_document_number": snapshot.accounting_document_number,
        },
        comment="Invoice finalized through the invoice-level workflow.",
    )
    return snapshot


@transaction.atomic
def submit_for_review(
    open_item: OpenItem,
    user: User | None = None,
    comments: str = "",
) -> ReviewAction:
    _require_classifiable(open_item)
    if open_item.status != OpenItem.Status.COMPLETED:
        raise ValueError("Only completed items can be submitted for review.")
    open_item.status = OpenItem.Status.READY_FOR_REVIEW
    open_item.save(update_fields=["status", "updated_at"])
    return _record_action(
        open_item,
        ReviewAction.ActionType.REVIEW_REQUESTED,
        user,
        comments=comments,
    )


@transaction.atomic
def start_review(
    open_item: OpenItem,
    user: User | None = None,
) -> ReviewAction:
    _require_classifiable(open_item)
    if open_item.status != OpenItem.Status.READY_FOR_REVIEW:
        raise ValueError("Only items ready for review can enter review.")
    open_item.status = OpenItem.Status.UNDER_REVIEW
    open_item.save(update_fields=["status", "updated_at"])
    return _record_action(open_item, ReviewAction.ActionType.REVIEW_STARTED, user)


@transaction.atomic
def approve_item(
    open_item: OpenItem,
    user: User | None = None,
    comments: str = "",
    override_reason: str = "",
) -> ReviewAction:
    return complete_review(
        open_item,
        user=user,
        comments=comments,
        override_reason=override_reason,
    )


@transaction.atomic
def complete_review(
    open_item: OpenItem,
    user: User | None = None,
    final_cost_type=None,
    comments: str = "",
    override_reason: str = "",
    manual_wht_rate=None,
) -> ReviewAction:
    _require_classifiable(open_item)
    if manual_wht_rate is not None:
        raise ValueError(
            "One-off WHT determinations must be approved through the Administrator Exceptions workflow."
        )
    if open_item.status != OpenItem.Status.UNDER_REVIEW:
        raise ValueError("Only items under review can be completed.")
    current_decision = (
        open_item.classification_decisions.filter(is_current=True).first()
    )
    if current_decision is None:
        raise ValueError("Review requires a current Final Cost Type.")
    determination = open_item.wht_determinations.order_by(
        "-calculated_at", "-id"
    ).first()
    if determination is None:
        raise ValueError("Review requires a WHT determination.")
    if final_cost_type is not None and final_cost_type != current_decision.final_cost_type:
        if not override_reason.strip():
            raise ValueError("An override reason is required when changing the Final Cost Type.")
        save_final_classification(
            open_item,
            final_cost_type,
            override_reason,
            user,
            allow_review_override=True,
        )
        updated_decision = open_item.classification_decisions.get(is_current=True)
    else:
        updated_decision = current_decision
    determination = open_item.wht_determinations.order_by(
        "-calculated_at", "-id"
    ).first()
    if determination.final_cost_type_snapshot != updated_decision.final_cost_type.code:
        raise ValueError(
            "The WHT determination is stale. An Administrator must explicitly recalculate it before a new review."
        )
    if not has_valid_determination(
        determination,
        expected_cost_type=updated_decision.final_cost_type.code,
    ):
        raise ValueError(
            "An unresolved WHT exception must be resolved by an Administrator before review completion."
        )

    open_item.status = OpenItem.Status.REVIEWED
    open_item.save(update_fields=["status", "updated_at"])
    required_lines = [
        line
        for line in OpenItem.objects.filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
        if is_open_item_classifiable(line)
    ]
    invoice_review_complete = all(
        line.pk == open_item.pk or line.status == OpenItem.Status.REVIEWED
        for line in required_lines
    )
    if invoice_review_complete:
        blockers = document_completion_blockers(open_item)
        if blockers:
            raise ValueError(
                "Invoice completion is blocked: " + " ".join(blockers)
            )
    action = _record_action(
        open_item,
        ReviewAction.ActionType.REVIEW_COMPLETED,
        user,
        comments=comments,
        override_reason=override_reason,
    )
    if invoice_review_complete:
        create_review_document_snapshot(
            open_item,
            completion_action=action,
            calculated_determination=determination,
        )
    return action


@transaction.atomic
def return_item(
    open_item: OpenItem,
    user: User | None = None,
    comments: str = "",
) -> ReviewAction:
    _require_classifiable(open_item)
    if open_item.status != OpenItem.Status.UNDER_REVIEW:
        raise ValueError("Only items under review can be returned.")
    if not comments.strip():
        raise ValueError("A return comment is required.")
    open_item.status = OpenItem.Status.RETURNED
    open_item.save(update_fields=["status", "updated_at"])
    return _record_action(
        open_item,
        ReviewAction.ActionType.RETURNED,
        user,
        comments=comments,
    )
