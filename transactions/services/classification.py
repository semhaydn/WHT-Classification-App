from django.contrib.auth.models import User
from django.db import transaction

from reference_data.models import CostType
from transactions.models import AuditEvent, ClassificationDecision, OpenItem


@transaction.atomic
def save_final_classification(
    open_item: OpenItem,
    final_cost_type: CostType,
    classification_reason: str,
    user: User | None = None,
    allow_review_override: bool = False,
) -> ClassificationDecision:
    if not open_item.is_classifiable:
        raise ValueError("Only non-zero postings on eligible G/L accounts can be classified.")
    if open_item.status in (
        OpenItem.Status.COMPLETED,
        OpenItem.Status.READY_FOR_REVIEW,
        OpenItem.Status.REVIEWED,
    ) or (
        open_item.status == OpenItem.Status.UNDER_REVIEW
        and not allow_review_override
    ):
        raise ValueError("Completed or reviewed items cannot be edited here.")
    previous = (
        ClassificationDecision.objects.filter(open_item=open_item, is_current=True)
        .select_related("final_cost_type")
        .first()
    )
    if previous is not None:
        previous.is_current = False
        previous.save(update_fields=["is_current"])

    decision = ClassificationDecision.objects.create(
        open_item=open_item,
        final_cost_type=final_cost_type,
        classification_method=(
            ClassificationDecision.Method.HUMAN_OVERRIDE
            if previous is not None or allow_review_override
            else ClassificationDecision.Method.HUMAN
        ),
        classification_reason=classification_reason,
        decided_by=user,
    )
    event_type = (
        AuditEvent.EventType.CLASSIFICATION_CHANGED
        if previous is not None
        else AuditEvent.EventType.CLASSIFICATION_SELECTED
    )
    AuditEvent.objects.create(
        open_item=open_item,
        event_type=event_type,
        performed_by=user,
        previous_value=(
            {"final_cost_type": previous.final_cost_type.code}
            if previous is not None
            else None
        ),
        new_value={"final_cost_type": final_cost_type.code},
        comment=classification_reason,
    )
    if open_item.status in (OpenItem.Status.NEW, OpenItem.Status.RETURNED):
        open_item.status = OpenItem.Status.IN_CLASSIFICATION
        open_item.save(update_fields=["status", "updated_at"])
    return decision


@transaction.atomic
def complete_classification(
    open_item: OpenItem,
    user: User | None = None,
) -> AuditEvent:
    if not open_item.is_classifiable:
        raise ValueError("Only non-zero postings on eligible G/L accounts can be completed.")
    if open_item.status != OpenItem.Status.IN_CLASSIFICATION:
        raise ValueError("Only items in classification can be completed.")
    if not open_item.classification_decisions.filter(is_current=True).exists():
        raise ValueError("A current Final Cost Type is required.")
    open_item.status = OpenItem.Status.COMPLETED
    open_item.save(update_fields=["status", "updated_at"])
    return AuditEvent.objects.create(
        open_item=open_item,
        event_type=AuditEvent.EventType.CLASSIFICATION_COMPLETED,
        performed_by=user,
        new_value={"status": OpenItem.Status.COMPLETED},
    )
