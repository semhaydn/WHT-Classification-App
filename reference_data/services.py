from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import WHTMapping


@transaction.atomic
def approve_wht_rule(rule_id: int, user: User) -> WHTMapping:
    if not user.is_superuser and not (
        user.is_staff and user.groups.filter(name="Administrator").exists()
    ):
        raise PermissionDenied("Only staff in the Administrator group can approve WHT rules.")

    rule = WHTMapping.objects.select_for_update().get(pk=rule_id)
    if rule.status != WHTMapping.Status.DRAFT:
        raise ValueError("Only draft WHT rules can be approved.")

    rule.status = WHTMapping.Status.APPROVED
    rule.approved_by = user
    rule.approved_at = timezone.now()
    rule.full_clean()
    rule.save(
        update_fields=["status", "approved_by", "approved_at"]
    )
    return rule
