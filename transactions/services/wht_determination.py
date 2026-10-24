from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.contrib.auth.models import User

from reference_data.models import SupplierCountry, WHTMapping
from transactions.models import ClassificationDecision, OpenItem, WHTDetermination
from transactions.permissions import has_role


@dataclass(frozen=True)
class WHTMatchResult:
    status: str
    candidate_count: int
    mapping: WHTMapping | None
    supplier_country: SupplierCountry | None


def resolve_document_supplier(open_item: OpenItem) -> tuple[str, str]:
    supplier_lines = list(
        OpenItem.objects.filter(
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
        .exclude(vendor_number="")
        .order_by()
        .values_list("vendor_number", "vendor_name")
        .distinct()
    )
    supplier_ids = {supplier_id for supplier_id, _ in supplier_lines}
    if len(supplier_ids) != 1:
        return "", ""
    supplier_id = next(iter(supplier_ids))
    supplier_names = {
        supplier_name
        for line_supplier_id, supplier_name in supplier_lines
        if line_supplier_id == supplier_id and supplier_name
    }
    return supplier_id, next(iter(supplier_names)) if len(supplier_names) == 1 else ""


def match_wht(open_item: OpenItem, decision: ClassificationDecision) -> WHTMatchResult:
    supplier_id, _ = resolve_document_supplier(open_item)
    supplier_country = SupplierCountry.objects.filter(
        supplier_id=supplier_id
    ).first()
    if supplier_country is None:
        return WHTMatchResult("REQUIRES_REVIEW", 0, None, None)
    candidates = list(
        WHTMapping.objects.filter(
            supplier=supplier_id,
            cost_category=decision.final_cost_type,
            country_code=supplier_country.country_code,
            status=WHTMapping.Status.APPROVED,
        )
        .order_by("-version", "pk")
    )
    if candidates:
        latest_version = candidates[0].version
        latest_candidates = [
            candidate for candidate in candidates
            if candidate.version == latest_version
        ]
        if len(latest_candidates) == 1:
            return WHTMatchResult(
                "MATCHED",
                1,
                latest_candidates[0],
                supplier_country,
            )
        return WHTMatchResult(
            "AMBIGUOUS",
            len(latest_candidates),
            None,
            supplier_country,
        )
    return WHTMatchResult("REQUIRES_REVIEW", 0, None, supplier_country)


@transaction.atomic
def determine_wht(
    open_item: OpenItem,
    decision: ClassificationDecision,
    manual_rate: Decimal | None = None,
    manual_reason: str = "",
    user: User | None = None,
    manual_no_wht: bool = False,
) -> WHTDetermination:
    if not open_item.is_classifiable:
        raise ValueError("WHT can only be determined for non-zero postings on eligible G/L accounts.")
    if manual_rate is not None and manual_no_wht:
        raise ValueError("Choose either a manual rate or an explicit No WHT decision.")
    if (manual_rate is not None or manual_no_wht) and not manual_reason.strip():
        raise ValueError("A reason is required for every manual WHT rate.")
    if manual_rate is not None and not manual_rate.is_finite():
        raise ValueError("Manual WHT rate must be a finite decimal.")
    if manual_rate is not None and manual_rate < 0:
        raise ValueError("Manual WHT rate cannot be negative.")
    if manual_rate == 0:
        raise ValueError(
            "Use an explicit No WHT decision instead of a zero one-off rate."
        )
    if (manual_rate is not None or manual_no_wht) and not has_role(user, "Administrator"):
        raise PermissionError(
            "Only an Administrator may approve a one-off WHT determination."
        )
    result = match_wht(open_item, decision)
    supplier_id, _ = resolve_document_supplier(open_item)
    base = open_item.document_currency_amount
    rate = (
        Decimal("0") if manual_no_wht
        else manual_rate
        if manual_rate is not None
        else (
            result.mapping.wht_rate
            if result.mapping is not None
            else Decimal("0") if result.status == "NO_WHT" else None
        )
    )
    amount = None
    status = (
        WHTDetermination.Status.NO_WHT
        if manual_no_wht
        else WHTDetermination.Status.ADMIN_RESOLVED
        if manual_rate is not None
        else result.status
    )
    if (
        result.mapping is not None
        and result.mapping.wht_rate == 0
        and manual_rate is None
    ):
        status = WHTDetermination.Status.NO_WHT
    if rate is not None:
        amount = (base * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return WHTDetermination.objects.create(
        open_item=open_item,
        status=status,
        candidate_count=result.candidate_count,
        matched_mapping=result.mapping,
        final_cost_type_snapshot=decision.final_cost_type.code,
        supplier_snapshot=supplier_id,
        supplier_country_snapshot=(
            result.supplier_country.country_code if result.supplier_country else ""
        ),
        rule_snapshot=(
            result.mapping.source_title if result.mapping is not None else ""
        ),
        rule_version_snapshot=(
            str(result.mapping.version)
            if result.mapping is not None
            else ""
        ),
        wht_rate_snapshot=rate,
        wht_base_snapshot=base,
        wht_amount_snapshot=amount,
        currency_snapshot=open_item.document_currency,
        rate_source=(
            "ADMIN_NO_WHT"
            if manual_no_wht
            else "MANUAL" if manual_rate is not None else "MAPPING"
        ),
        manual_override_reason=manual_reason,
        calculation_version=(
            open_item.wht_determinations.order_by("-calculated_at", "-id")
            .values_list("calculation_version", flat=True)
            .first()
            or 0
        ) + 1,
        calculated_by=user,
    )
