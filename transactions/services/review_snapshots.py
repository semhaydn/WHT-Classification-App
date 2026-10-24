from decimal import Decimal
from django.db.models import Max
from django.db.models import Q
from datetime import date

from django.db import transaction

from reference_data.models import SupplierCountry
from transactions.models import (
    ClassificationDecision,
    AuditEvent,
    LineExceptionRecord,
    OpenItem,
    ReviewAction,
    ReviewDocumentSnapshot,
    ReviewLineCalculationSnapshot,
    WHTDetermination,
)
from transactions.services.reconciliation import (
    has_valid_determination,
    reconcile_amounts,
    reconcile_document,
    reconciliation_status_label,
    round_money,
)
from transactions.services.gl_eligibility import (
    get_gl_eligibility_map,
    is_open_item_classifiable,
)
from transactions.services.invoice_amounts import invoice_amount_from_control_lines
from transactions.services.wht_determination import resolve_document_supplier


ZERO = Decimal("0.00")


def _current_decision(item: OpenItem) -> ClassificationDecision | None:
    return (
        item.classification_decisions.filter(is_current=True)
        .select_related("final_cost_type")
        .first()
    )


@transaction.atomic
def create_review_document_snapshot(
    completed_item: OpenItem,
    *,
    completion_action: ReviewAction,
    calculated_determination: WHTDetermination | None = None,
) -> ReviewDocumentSnapshot:
    if completed_item.status != OpenItem.Status.REVIEWED:
        raise ValueError("A completed review is required to create a snapshot.")
    if (
        completion_action.action_type != ReviewAction.ActionType.REVIEW_COMPLETED
        or completion_action.open_item.entity_code != completed_item.entity_code
        or completion_action.open_item.accounting_document_number
        != completed_item.accounting_document_number
    ):
        raise ValueError(
            "The review completion action must match the completed document."
        )
    lines = list(
        OpenItem.objects.filter(
            entity_code=completed_item.entity_code,
            accounting_document_number=completed_item.accounting_document_number,
        ).select_related("source_import_batch")
    )
    invoice_lines = [line for line in lines if line.gl_account == "210000"]
    wht_posting_lines = [line for line in lines if line.gl_account == "229220"]
    gl_eligibility = get_gl_eligibility_map(line.gl_account for line in lines)
    excluded_item_ids = set(
        LineExceptionRecord.objects.filter(
            open_item_id__in=[line.pk for line in lines],
            status=LineExceptionRecord.Status.EXCLUDED,
        ).values_list("open_item_id", flat=True)
    )
    eligible_lines = [
        line
        for line in lines
        if is_open_item_classifiable(line, gl_eligibility, excluded_item_ids)
    ]
    exceptions: list[str] = []
    mapping_required = False

    invoice_currencies = {line.document_currency for line in invoice_lines}
    document_currency = next(iter(invoice_currencies)) if len(invoice_currencies) == 1 else ""
    document_amount = invoice_amount_from_control_lines(invoice_lines)
    if not invoice_lines:
        exceptions.append("No GL 210000 vendor invoice/control posting was found.")
    elif len(invoice_currencies) > 1:
        exceptions.append(
            "GL 210000 invoice/control lines use multiple document currencies."
        )

    supplier_ids = {line.vendor_number for line in lines if line.vendor_number}
    supplier_names = {
        line.vendor_name
        for line in lines
        if line.vendor_number and line.vendor_name
    }
    if len(supplier_ids) != 1:
        supplier = ""
        vendor_name = ""
        supplier_country = ""
        mapping_required = True
        exceptions.append(
            "Supplier is missing or ambiguous across the accounting document."
        )
    else:
        supplier = next(iter(supplier_ids))
        matching_names = {
            line.vendor_name
            for line in lines
            if line.vendor_number == supplier and line.vendor_name
        }
        vendor_name = next(iter(matching_names)) if len(matching_names) == 1 else ""
        country_record = SupplierCountry.objects.filter(supplier_id=supplier).first()
        supplier_country = country_record.country_code if country_record else ""
        if not supplier_country:
            exceptions.append(f"Supplier country is missing for supplier {supplier}.")
        if len(supplier_names) > 1 and not vendor_name:
            exceptions.append("Vendor name is inconsistent across the accounting document.")

    eligible_currencies = {line.document_currency for line in eligible_lines}
    if document_currency and any(
        currency != document_currency for currency in eligible_currencies
    ):
        exceptions.append(
            "Eligible expense lines use a different currency from the invoice/control posting."
        )

    line_results: list[dict[str, object]] = []
    determinations: dict[int, WHTDetermination | None] = {}
    expected_cost_types: dict[int, str | None] = {}
    determination_countries: set[str] = set()
    for line in eligible_lines:
        decision = _current_decision(line)
        line_exception = ""
        line_supplier, _ = resolve_document_supplier(line)
        line_country = supplier_country
        determination = None

        if decision is None:
            line_exception = "Reviewer-approved Final Cost Type is missing."
            mapping_required = True
        else:
            if line.pk == completed_item.pk and calculated_determination is not None:
                determination = calculated_determination
            else:
                determination = line.wht_determinations.order_by(
                    "-calculated_at", "-id"
                ).first()
            expected_cost_types[line.pk] = decision.final_cost_type.code
            determinations[line.pk] = determination
            if determination is not None:
                line_supplier = determination.supplier_snapshot or supplier
                line_country = determination.supplier_country_snapshot or supplier_country
                if determination.supplier_country_snapshot:
                    determination_countries.add(
                        determination.supplier_country_snapshot
                    )
            if determination is not None:
                valid_determination = has_valid_determination(
                    determination,
                    expected_cost_type=decision.final_cost_type.code,
                )
                if not valid_determination:
                    mapping_required = True
                    if determination.status == WHTDetermination.Status.AMBIGUOUS:
                        line_exception = (
                            "Multiple approved mappings share the latest version "
                            "for the exact supplier, country, and Final Cost Type."
                        )
                    else:
                        line_exception = (
                            "No unique approved WHT mapping matches the exact "
                            "supplier, country, and Final Cost Type."
                        )
            else:
                line_exception = "A WHT determination is missing."
                mapping_required = True
        if line_exception:
            exceptions.append(
                f"Line {line.line_number}: {line_exception}"
            )

        line_amount = (
            determination.wht_amount_snapshot
            if determination is not None
            and decision is not None
            and has_valid_determination(
                determination,
                expected_cost_type=decision.final_cost_type.code,
            )
            else None
        )
        if (
            determination is not None
            and determination.currency_snapshot != document_currency
        ) or line.document_currency != document_currency:
            line_exception = line_exception or (
                "Expense-line currency does not match the document currency."
            )
            line_amount = None
        line_results.append(
            {
                "open_item": line,
                "decision": decision,
                "supplier": line_supplier,
                "country": line_country,
                "determination": determination,
                "line_amount": line_amount,
                "exception": line_exception,
            }
        )

    if len(determination_countries) == 1:
        supplier_country = next(iter(determination_countries))
        exceptions = [
            reason
            for reason in exceptions
            if not reason.startswith("Supplier country is missing for supplier ")
        ]
    reconciliation = reconcile_document(
        lines,
        eligible_lines,
        determinations,
        document_currency,
        expected_cost_types,
    )
    calculated_wht = reconciliation.calculated_wht
    booked_wht = reconciliation.posted_wht
    modification = reconciliation.difference
    if booked_wht is None:
        exceptions.append(
            "GL 229220 WHT postings cannot be reconciled in one document currency."
        )
    if not eligible_lines:
        exceptions.append("No non-zero eligible G/L lines were found.")

    previous = ReviewDocumentSnapshot.objects.filter(
        entity_code=completed_item.entity_code,
        accounting_document_number=completed_item.accounting_document_number,
        is_current=True,
    ).first()
    latest_version = ReviewDocumentSnapshot.objects.filter(
        entity_code=completed_item.entity_code,
        accounting_document_number=completed_item.accounting_document_number,
    ).aggregate(version=Max("calculation_version"))["version"]
    version = (latest_version or 0) + 1
    if previous:
        previous.is_current = False
        previous.save(update_fields=["is_current"])

    snapshot = ReviewDocumentSnapshot.objects.create(
        entity_code=completed_item.entity_code,
        accounting_document_number=completed_item.accounting_document_number,
        supplier=supplier,
        vendor_name=vendor_name,
        supplier_country=supplier_country,
        document_amount=document_amount,
        document_currency=document_currency,
        calculated_wht=calculated_wht,
        booked_wht=booked_wht,
        has_wht_posting=bool(wht_posting_lines),
        modification_required=modification,
        mapping_required=mapping_required,
        exception_reason=" ".join(dict.fromkeys(exceptions)),
        review_status=(
            ReviewDocumentSnapshot.ReviewStatus.REVIEWED
            if all(
                line.status == OpenItem.Status.REVIEWED
                for line in eligible_lines
            )
            else ReviewDocumentSnapshot.ReviewStatus.PARTIALLY_REVIEWED
        ),
        completed_by=completion_action.performed_by,
        completed_at=completion_action.created_at,
        source_import_batch=completed_item.source_import_batch,
        calculation_version=version,
    )
    for result in line_results:
        line = result["open_item"]
        decision = result["decision"]
        determination = result["determination"]
        ReviewLineCalculationSnapshot.objects.create(
            document_snapshot=snapshot,
            open_item=line,
            line_number=line.line_number,
            supplier=result["supplier"],
            supplier_country=result["country"],
            final_cost_type=(
                decision.final_cost_type.code if decision is not None else ""
            ),
            eligible_service_amount=line.document_currency_amount,
            document_currency=line.document_currency,
            mapping=(
                determination.matched_mapping
                if determination is not None
                else None
            ),
            mapping_version=(
                determination.rule_version_snapshot
                if determination is not None
                else ""
            ),
            wht_determination=determination,
            wht_rate=(
                determination.wht_rate_snapshot
                if determination is not None
                else None
            ),
            calculated_line_wht=result["line_amount"],
            exception_reason=result["exception"],
            source_import_batch=line.source_import_batch,
        )
    if latest_version is not None:
        AuditEvent.objects.create(
            open_item=completed_item,
            event_type=AuditEvent.EventType.INVOICE_RECOMPLETED,
            performed_by=completion_action.performed_by,
            previous_value={"snapshot_version": latest_version},
            new_value={
                "entity_code": snapshot.entity_code,
                "accounting_document_number": snapshot.accounting_document_number,
                "snapshot_id": snapshot.pk,
                "snapshot_version": snapshot.calculation_version,
                "completed_at": snapshot.completed_at.isoformat(),
            },
            comment="Invoice re-completed; prior snapshots remain in history.",
        )
    return snapshot


def completed_review_snapshots(filters: dict[str, str] | None = None):
    snapshots = (
        ReviewDocumentSnapshot.objects.filter(
            is_current=True,
            review_status=ReviewDocumentSnapshot.ReviewStatus.REVIEWED,
        )
        .select_related("completed_by")
        .order_by("entity_code", "accounting_document_number")
    )
    if not filters:
        return snapshots
    if filters.get("entity"):
        snapshots = snapshots.filter(entity_code=filters["entity"])
    if filters.get("document"):
        snapshots = snapshots.filter(
            accounting_document_number__icontains=filters["document"]
        )
    if filters.get("supplier"):
        snapshots = snapshots.filter(
            Q(supplier__icontains=filters["supplier"])
            | Q(vendor_name__icontains=filters["supplier"])
        )
    if filters.get("country"):
        snapshots = snapshots.filter(supplier_country=filters["country"].upper())
    if filters.get("currency"):
        snapshots = snapshots.filter(document_currency=filters["currency"].upper())
    if filters.get("mapping_required") == "1":
        snapshots = snapshots.filter(mapping_required=True)
    if filters.get("status") and filters["status"] != "Completed":
        snapshots = snapshots.none()
    if filters.get("date_from"):
        try:
            snapshots = snapshots.filter(
                completed_at__date__gte=date.fromisoformat(filters["date_from"])
            )
        except ValueError:
            pass
    if filters.get("date_to"):
        try:
            snapshots = snapshots.filter(
                completed_at__date__lte=date.fromisoformat(filters["date_to"])
            )
        except ValueError:
            pass
    if filters.get("reconciliation_status"):
        snapshots = [
            snapshot
            for snapshot in snapshots
            if serialize_review_snapshot(snapshot)["reconciliation_status"]
            == {
                "RECONCILED": "Reconciled",
                "DISCREPANCY": "Discrepancy",
                "PENDING": "Pending / Provisional",
            }.get(filters["reconciliation_status"], filters["reconciliation_status"])
        ]
    return snapshots


def serialize_review_snapshot(snapshot: ReviewDocumentSnapshot) -> dict[str, object]:
    _, reconciliation_status = reconcile_amounts(
        snapshot.calculated_wht,
        snapshot.booked_wht,
    )
    return {
        "entity_code": snapshot.entity_code,
        "accounting_document_number": snapshot.accounting_document_number,
        "supplier": snapshot.supplier,
        "vendor_name": snapshot.vendor_name,
        "country": snapshot.supplier_country,
        "document_amount": snapshot.document_amount,
        "document_currency": snapshot.document_currency,
        "calculated_wht": snapshot.calculated_wht,
        "booked_wht": snapshot.booked_wht,
        "modification_required": snapshot.modification_required,
        "reconciliation_status": reconciliation_status_label(
            reconciliation_status
        ),
        "review_status": snapshot.get_review_status_display(),
        "mapping_required": snapshot.mapping_required,
        "exception_status": (
            snapshot.exception_reason
            if snapshot.exception_reason
            else "Mapping required" if snapshot.mapping_required else "Resolved"
        ),
        "completed_by": (
            snapshot.completed_by.get_full_name()
            or snapshot.completed_by.username
            if snapshot.completed_by
            else ""
        ),
        "completed_at": snapshot.completed_at,
    }
