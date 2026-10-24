from dataclasses import dataclass

from django.contrib.auth.models import User

from transactions.models import AuditEvent, MaterialData, MaterialMatch, OpenItem


@dataclass(frozen=True)
class MaterialMatchResult:
    status: str
    candidate_count: int
    candidates: tuple[MaterialData, ...]


def get_stored_material_match(open_item: OpenItem) -> MaterialMatchResult:
    match_record = (
        MaterialMatch.objects.filter(open_item=open_item)
        .select_related("material_data")
        .first()
    )
    if match_record is None:
        return MaterialMatchResult("NOT_FOUND", 0, ())
    candidates = (
        (match_record.material_data,)
        if match_record.status == MaterialMatch.Status.MATCHED
        and match_record.material_data_id
        else ()
    )
    return MaterialMatchResult(
        status=match_record.status,
        candidate_count=match_record.candidate_count,
        candidates=candidates,
    )


def match_material(
    open_item: OpenItem,
    performed_by: User | None = None,
) -> MaterialMatchResult:
    if not open_item.purchasing_document or not open_item.purchasing_document_item:
        candidates = ()
    else:
        candidates = tuple(
            MaterialData.objects.filter(
                purchasing_document=open_item.purchasing_document,
                item=open_item.purchasing_document_item,
            )
        )
    if len(candidates) == 1:
        status = "MATCHED"
    elif candidates:
        status = "AMBIGUOUS"
    else:
        status = "NOT_FOUND"
    result = MaterialMatchResult(
        status=status,
        candidate_count=len(candidates),
        candidates=candidates,
    )
    selected_material = candidates[0] if status == "MATCHED" else None
    match_record = (
        MaterialMatch.objects.filter(open_item=open_item)
        .select_related("material_data")
        .first()
    )
    changed = match_record is None or (
        match_record.status != status
        or match_record.candidate_count != len(candidates)
        or match_record.material_data_id
        != (selected_material.pk if selected_material is not None else None)
    )
    previous_value = None
    if match_record is not None and changed:
        previous_value = {
            "status": match_record.status,
            "material_data_key": (
                match_record.material_data.material_data_key
                if match_record.material_data_id
                else ""
            ),
        }
    if changed:
        MaterialMatch.objects.create(
            open_item=open_item,
            material_data=selected_material,
            status=status,
            candidate_count=len(candidates),
        )
        event_type = {
            "MATCHED": AuditEvent.EventType.MATERIAL_MATCHED,
            "NOT_FOUND": AuditEvent.EventType.MATERIAL_MATCH_NOT_FOUND,
            "AMBIGUOUS": AuditEvent.EventType.MATERIAL_MATCH_AMBIGUOUS,
        }[status]
        AuditEvent.objects.create(
            open_item=open_item,
            event_type=event_type,
            performed_by=performed_by,
            previous_value=previous_value,
            new_value={
                "status": status,
                "candidate_count": len(candidates),
                "material_data_key": (
                    selected_material.material_data_key
                    if selected_material is not None
                    else ""
                ),
            },
        )
    elif match_record is not None:
        match_record.save(update_fields=["last_checked_at"])
    return result
