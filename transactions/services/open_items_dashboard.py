from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from django.db.models import Prefetch, QuerySet

from reference_data.models import SupplierCountry
from transactions.models import ClassificationDecision, OpenItem, WHTDetermination
from transactions.models import LineExceptionRecord
from transactions.services.reconciliation import (
    has_valid_determination,
    reconcile_document,
)
from transactions.services.gl_eligibility import (
    get_gl_eligibility_map,
    is_open_item_classifiable,
)
from transactions.services.invoice_amounts import invoice_amount_from_control_lines


@dataclass(frozen=True)
class DocumentSummary:
    entity_code: str
    document_number: str
    lines: tuple[OpenItem, ...]
    vendor_number: str
    vendor_name: str
    country: str
    currency: str
    invoice_amount: Decimal | None
    calculated_wht: Decimal | None
    posted_wht: Decimal | None
    difference: Decimal | None
    reconciliation_status: str
    mapping_required: bool
    workflow_status: str
    document_date: object | None
    posting_date: object | None
    lines_requiring_work: int
    outstanding_exception_count: int
    exception_item_ids: tuple[int, ...]
    completion_blockers: tuple[str, ...]


def dashboard_queryset() -> QuerySet[OpenItem]:
    return OpenItem.objects.select_related(
    ).prefetch_related(
        Prefetch(
            "classification_decisions",
            queryset=ClassificationDecision.objects.filter(is_current=True).select_related(
                "final_cost_type"
            ),
            to_attr="dashboard_classifications",
        ),
        Prefetch(
            "wht_determinations",
            queryset=WHTDetermination.objects.order_by("-calculated_at", "-id"),
            to_attr="dashboard_determinations",
        ),
        "review_actions",
    ).order_by("entity_code", "accounting_document_number", "line_number")


def summarize_documents(items: Iterable[OpenItem]) -> list[DocumentSummary]:
    items = list(items)
    grouped: dict[tuple[str, str], list[OpenItem]] = {}
    for item in items:
        grouped.setdefault(
            (item.entity_code, item.accounting_document_number), []
        ).append(item)

    supplier_ids = {
        line.vendor_number
        for lines in grouped.values()
        for line in lines
        if line.vendor_number
    }
    countries = {
        country.supplier_id: country
        for country in SupplierCountry.objects.filter(
            supplier_id__in=supplier_ids
        )
    }
    gl_eligibility = get_gl_eligibility_map(line.gl_account for line in items)
    excluded_item_ids = set(
        LineExceptionRecord.objects.filter(
            open_item_id__in=[line.pk for line in items],
            status=LineExceptionRecord.Status.EXCLUDED,
        ).values_list("open_item_id", flat=True)
    )
    open_flag_item_ids = set(
        LineExceptionRecord.objects.filter(
            open_item_id__in=[line.pk for line in items],
            issue_type=LineExceptionRecord.Type.USER_FLAG,
            status=LineExceptionRecord.Status.OPEN,
        ).values_list("open_item_id", flat=True)
    )
    summaries: list[DocumentSummary] = []
    for (entity_code, document_number), lines in grouped.items():
        currencies = {line.document_currency for line in lines}
        currency = currencies.pop() if len(currencies) == 1 else ""
        invoice_lines = [line for line in lines if line.gl_account == "210000"]
        eligible = [
            line
            for line in lines
            if is_open_item_classifiable(
                line,
                gl_eligibility,
                excluded_item_ids,
            )
        ]
        document_supplier_ids = {line.vendor_number for line in lines if line.vendor_number}
        supplier_ambiguous = len(document_supplier_ids) > 1
        supplier = (
            next(iter(document_supplier_ids))
            if len(document_supplier_ids) == 1
            else ""
        )
        vendor_names = {
            line.vendor_name
            for line in lines
            if line.vendor_number == supplier and line.vendor_name
        }
        vendor_name = next(iter(vendor_names)) if len(vendor_names) == 1 else ""
        supplier_country = countries.get(supplier)
        country = supplier_country.country_code if supplier_country else ""
        mapping_required = supplier_ambiguous
        determinations = {}
        expected_cost_types = {}
        lines_requiring_work = 0
        wht_exception_item_ids: set[int] = set()
        completion_blockers: list[str] = []
        if not eligible:
            completion_blockers.append(
                "No non-zero eligible invoice lines are available for finalization."
            )
        for line in eligible:
            classifications = getattr(line, "dashboard_classifications", [])
            latest_determinations = getattr(line, "dashboard_determinations", None)
            if latest_determinations is None:
                latest_determinations = list(
                    line.wht_determinations.order_by("-calculated_at", "-id")[:1]
                )
            determination = (
                latest_determinations[0] if latest_determinations else None
            )
            determinations[line.pk] = determination
            expected_cost_type = (
                classifications[0].final_cost_type.code
                if classifications
                else None
            )
            expected_cost_types[line.pk] = expected_cost_type
            if not has_valid_determination(
                determination,
                expected_cost_type=expected_cost_type,
            ):
                mapping_required = True
                if classifications:
                    wht_exception_item_ids.add(line.pk)
            if not classifications:
                completion_blockers.append(
                    f"Line {line.line_number}: Final Cost Type is missing."
                )
            elif determination is None:
                completion_blockers.append(
                    f"Line {line.line_number}: WHT determination is missing."
                )
            elif determination.final_cost_type_snapshot != classifications[0].final_cost_type.code:
                completion_blockers.append(
                    f"Line {line.line_number}: WHT determination is stale."
                )
            elif not has_valid_determination(
                determination,
                expected_cost_type=classifications[0].final_cost_type.code,
            ):
                completion_blockers.append(
                    f"Line {line.line_number}: WHT determination is unresolved."
                )
            if line.status not in (
                OpenItem.Status.COMPLETED,
                OpenItem.Status.READY_FOR_REVIEW,
                OpenItem.Status.UNDER_REVIEW,
                OpenItem.Status.REVIEWED,
            ):
                completion_blockers.append(
                    f"Line {line.line_number}: classification is unfinished."
                )
            if (
                not classifications
                or line.status != OpenItem.Status.REVIEWED
            ):
                lines_requiring_work += 1
        document_flags = open_flag_item_ids.intersection(
            line.pk for line in lines
        )
        exception_item_ids = set(document_flags) | wht_exception_item_ids
        if document_flags:
            completion_blockers.append(
                "An unresolved Administrator review flag remains."
            )
        blockers = tuple(completion_blockers)
        document_lines = [line for line in lines if line.gl_account == "210000"]
        dates = {line.document_date for line in document_lines}
        document_date = next(iter(dates)) if len(dates) == 1 else None
        posting_dates = [line.posting_date for line in lines if line.posting_date]
        posting_date = max(posting_dates) if posting_dates else None
        reconciliation = reconcile_document(
            lines,
            eligible,
            determinations,
            currency,
            expected_cost_types,
        )
        summaries.append(
            DocumentSummary(
                entity_code=entity_code,
                document_number=document_number,
                lines=tuple(lines),
                vendor_number=supplier,
                vendor_name=vendor_name,
                country=country,
                currency=currency,
                invoice_amount=invoice_amount_from_control_lines(invoice_lines),
                calculated_wht=reconciliation.calculated_wht,
                posted_wht=reconciliation.posted_wht,
                difference=reconciliation.difference,
                reconciliation_status=reconciliation.status,
                mapping_required=mapping_required,
                workflow_status=(
                    "Blocked"
                    if blockers
                    else "Review pending"
                    if lines_requiring_work
                    else "Review complete"
                ),
                document_date=document_date,
                posting_date=posting_date,
                lines_requiring_work=lines_requiring_work,
                outstanding_exception_count=len(exception_item_ids),
                exception_item_ids=tuple(sorted(exception_item_ids)),
                completion_blockers=blockers,
            )
        )
    return summaries
