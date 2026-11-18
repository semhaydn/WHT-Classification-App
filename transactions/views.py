import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.contrib import messages
from django.core import signing
from django.core.signing import BadSignature
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.core.management.base import CommandError
from django.conf import settings
from datetime import date
from urllib.parse import urlencode

from reference_data.models import CostType
from .forms import (
    ClassificationDecisionForm,
    LineExclusionForm,
    LineFlagForm,
    LineFlagResolutionForm,
    WHTExceptionResolutionForm,
)
from .models import (
    AuditEvent,
    ImportBatch,
    ImportUploadSession,
    LineExceptionRecord,
    OpenItem,
    ReviewDocumentSnapshot,
)
from .import_forms import ImportCenterForm
from .services.import_center import (
    cleanup_expired_uploads,
    confirm_import,
    get_batch_match_counts,
    preview_and_stage,
)
from .services.classification import complete_classification, save_final_classification
from .services.cost_type_ai.provider import RecommendationUnavailable
from .services.cost_type_ai.recommendation import recommend_cost_types
from .services.material_matching import get_stored_material_match
from .services.review import finalize_invoice
from .services.wht_determination import determine_wht
from .permissions import has_role, role_required
from .services.open_items_dashboard import dashboard_queryset, summarize_documents
from .services.open_items_export import build_workbook
from .services.completed_review_export import build_completed_review_workbook
from .services.review_snapshots import (
    completed_review_snapshots,
    serialize_review_snapshot,
)
from .services.reconciliation import (
    determination_status_label,
    reconciliation_status_label,
)
from .services.exceptions import (
    build_exception_queue,
    document_completion_blockers,
    exclude_line,
    flag_line_for_review,
    recalculate_wht_exception,
    resolve_line_flag,
    resolve_wht_exception,
)
from .services.reopening import reopen_invoice, reopen_line
from .services.gl_eligibility import is_open_item_classifiable

logger = logging.getLogger(__name__)
_COST_TYPE_RECOMMENDATION_SALT = "transactions.cost_type_ai.recommendation"
_COST_TYPE_RECOMMENDATION_MAX_AGE = 15 * 60


def _signed_recommendation_cost_type(
    token: str,
    open_item: OpenItem,
) -> CostType | None:
    if not token:
        return None
    try:
        payload = signing.loads(
            token,
            salt=_COST_TYPE_RECOMMENDATION_SALT,
            max_age=_COST_TYPE_RECOMMENDATION_MAX_AGE,
        )
    except BadSignature:
        logger.warning("Discarded invalid signed Cost Type recommendation.")
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("open_item_id") != open_item.pk
        or not isinstance(payload.get("cost_type_code"), str)
    ):
        logger.warning("Discarded mismatched signed Cost Type recommendation.")
        return None
    return CostType.objects.filter(
        code=payload["cost_type_code"],
        active=True,
    ).first()


class DashboardRows(list):
    def count(self, value=None):
        return len(self) if value is None else super().count(value)


def _query_string(filters: dict[str, str], **overrides: str) -> str:
    values = {key: value for key, value in filters.items() if value}
    values.update(overrides)
    return urlencode({key: value for key, value in values.items() if value})


def _invoice_list_filters(request: HttpRequest) -> dict[str, str]:
    return {
        "entity": request.GET.get("entity", "").strip(),
        "supplier": request.GET.get(
            "supplier",
            request.GET.get("vendor", ""),
        ).strip(),
        "document": request.GET.get("document", "").strip(),
        "date_from": request.GET.get("date_from", "").strip(),
        "date_to": request.GET.get("date_to", "").strip(),
        "status": request.GET.get("status", "").strip(),
        "reconciliation_status": request.GET.get(
            "reconciliation_status", ""
        ).strip(),
        "exception": request.GET.get("exception", "").strip(),
        "country": request.GET.get("country", "").strip().upper(),
        "currency": request.GET.get("currency", "").strip().upper(),
        "sort": request.GET.get("sort", "").strip(),
    }


def _date_filter_match(value, date_from: str, date_to: str) -> bool:
    if date_from:
        try:
            if value is None or value < date.fromisoformat(date_from):
                return False
        except ValueError:
            pass
    if date_to:
        try:
            if value is None or value > date.fromisoformat(date_to):
                return False
        except ValueError:
            pass
    return True


def _active_invoice_rows(filters: dict[str, str]) -> list[dict[str, object]]:
    summaries = summarize_documents(dashboard_queryset())
    completed_keys = set(
        ReviewDocumentSnapshot.objects.filter(
            is_current=True,
            review_status=ReviewDocumentSnapshot.ReviewStatus.REVIEWED,
        ).values_list("entity_code", "accounting_document_number")
    )
    rows: list[dict[str, object]] = []
    for summary in summaries:
        if (summary.entity_code, summary.document_number) in completed_keys:
            continue
        invoice_lines = [line for line in summary.lines if line.gl_account == "210000"]
        status = summary.workflow_status
        if filters["entity"] and summary.entity_code != filters["entity"]:
            continue
        supplier_search = filters["supplier"].casefold()
        if supplier_search and not (
            supplier_search in summary.vendor_number.casefold()
            or supplier_search in summary.vendor_name.casefold()
        ):
            continue
        if filters["document"] and filters["document"].casefold() not in summary.document_number.casefold():
            continue
        if not _date_filter_match(
            summary.posting_date,
            filters["date_from"],
            filters["date_to"],
        ):
            continue
        if filters["country"] and summary.country != filters["country"]:
            continue
        if filters["currency"] and summary.currency != filters["currency"]:
            continue
        status_filter = filters["status"]
        if status_filter and status_filter not in {
            status,
            *(line.status for line in summary.lines),
        }:
            continue
        if (
            filters["reconciliation_status"]
            and summary.reconciliation_status != filters["reconciliation_status"]
        ):
            continue
        if filters["exception"] == "1" and not summary.outstanding_exception_count:
            continue
        if filters["exception"] == "0" and summary.outstanding_exception_count:
            continue
        rows.append(
            {
                "entity_code": summary.entity_code,
                "accounting_document_number": summary.document_number,
                "supplier": summary.vendor_number,
                "vendor_name": summary.vendor_name,
                "country": summary.country,
                "document_date": summary.document_date,
                "posting_date": summary.posting_date,
                "document_currency": summary.currency,
                "invoice_currency": summary.currency,
                "document_amount": summary.invoice_amount,
                "invoice_amount": summary.invoice_amount,
                "workflow_status": status,
                "lines_requiring_work": summary.lines_requiring_work,
                "outstanding_exception_count": summary.outstanding_exception_count,
                "calculated_wht": summary.calculated_wht,
                "posted_wht": summary.posted_wht,
                "difference": summary.difference,
                "reconciliation_status": reconciliation_status_label(
                    summary.reconciliation_status
                ),
                "completion_status": (
                    summary.completion_blockers[0]
                    if summary.completion_blockers
                    else "No blocking exception"
                    if not summary.outstanding_exception_count
                    else "Review exceptions"
                ),
                "completed_at": None,
                "exception_item_id": (
                    summary.exception_item_ids[0]
                    if summary.exception_item_ids
                    else None
                ),
                "line_ids": tuple(line.pk for line in summary.lines),
                "line_statuses": tuple(line.status for line in summary.lines),
            }
        )
    return rows


@role_required("Transaction User", "Administrator")
def open_item_list(request: HttpRequest) -> HttpResponse:
    if "queue" in request.GET:
        query = request.GET.copy()
        query.pop("queue", None)
        target = reverse("open-item-list")
        if query:
            target = f"{target}?{query.urlencode()}"
        return redirect(target)
    filters = _invoice_list_filters(request)
    documents = _active_invoice_rows(filters)
    sort = filters["sort"]
    sort_key = sort.lstrip("-")
    sort_fields = {
        "document": "accounting_document_number",
        "supplier": "vendor_name",
        "posting_date": "posting_date",
        "amount": "document_amount",
    }
    if sort_key in sort_fields:
        documents.sort(
            key=lambda row: row[sort_fields[sort_key]] or "",
            reverse=sort.startswith("-"),
        )
    paginator = Paginator(documents, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    filter_query = _query_string(filters)
    return render(
        request,
        "transactions/open_item_list.html",
        {
            "open_items": DashboardRows(documents),
            "documents": page_obj,
            "page_obj": page_obj,
            "filters": filters,
            "status_choices": (
                ("Blocked", "Blocked"),
                ("Review pending", "Review pending"),
                ("Review complete", "Review complete"),
            ),
            "reconciliation_choices": [
                ("RECONCILED", "Reconciled"),
                ("DISCREPANCY", "Discrepancy"),
                ("PENDING", "Pending / Provisional"),
            ],
            "filter_query": filter_query,
            "invoice_rows": documents,
        },
    )


@role_required("Transaction User", "Administrator")
def export_open_items(request: HttpRequest) -> HttpResponse:
    filters = _invoice_list_filters(request)
    workbook = build_workbook(_active_invoice_rows(filters), filters)
    response = HttpResponse(
        workbook.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="active-invoices.xlsx"'
    return response


@role_required("Transaction User", "Administrator")
def review_completed_items(request: HttpRequest) -> HttpResponse:
    filters = _invoice_list_filters(request)
    snapshots = completed_review_snapshots(filters)
    documents = []
    for snapshot in snapshots:
        document = serialize_review_snapshot(snapshot)
        document.update(
            {
                "document_date": None,
                "posting_date": None,
                "country": document["country"],
                "workflow_status": "Completed",
                "lines_requiring_work": 0,
                "outstanding_exception_count": int(snapshot.mapping_required),
                "completion_status": document["exception_status"],
            }
        )
        if filters["status"] and filters["status"] != "Completed":
            continue
        if filters["exception"] == "1" and not snapshot.mapping_required:
            continue
        if filters["exception"] == "0" and snapshot.mapping_required:
            continue
        documents.append(document)
    if filters["sort"] in {"document", "-document", "supplier", "-supplier"}:
        sort_field = (
            "accounting_document_number"
            if filters["sort"].endswith("document")
            or filters["sort"].startswith("-document")
            else "vendor_name"
        )
        documents.sort(
            key=lambda row: str(row[sort_field] or ""),
            reverse=filters["sort"].startswith("-"),
        )
    page_obj = Paginator(documents, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "transactions/review_completed_items.html",
        {
            "page_obj": page_obj,
            "filters": filters,
            "filter_query": _query_string(filters),
            "reconciliation_choices": [
                ("RECONCILED", "Reconciled"),
                ("DISCREPANCY", "Discrepancy"),
                ("PENDING", "Pending / Provisional"),
            ],
        },
    )


@role_required("Transaction User", "Administrator")
def export_review_completed_items(request: HttpRequest) -> HttpResponse:
    filters = _invoice_list_filters(request)
    snapshots = list(completed_review_snapshots(filters))
    if filters["status"] and filters["status"] != "Completed":
        snapshots = []
    if filters["exception"] == "1":
        snapshots = [snapshot for snapshot in snapshots if snapshot.mapping_required]
    elif filters["exception"] == "0":
        snapshots = [snapshot for snapshot in snapshots if not snapshot.mapping_required]
    if filters["sort"] in {"document", "-document", "supplier", "-supplier"}:
        sort_field = (
            "accounting_document_number"
            if "document" in filters["sort"]
            else "vendor_name"
        )
        snapshots.sort(
            key=lambda snapshot: getattr(snapshot, sort_field) or "",
            reverse=filters["sort"].startswith("-"),
        )
    workbook = build_completed_review_workbook(
        snapshots,
        filters,
    )
    response = HttpResponse(
        workbook.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        'attachment; filename="completed-invoices.xlsx"'
    )
    return response


@role_required("Transaction User", "Administrator")
def open_item_document(
    request: HttpRequest,
    entity_code: str,
    accounting_document_number: str,
) -> HttpResponse:
    open_items = OpenItem.objects.filter(
        entity_code=entity_code,
        accounting_document_number=accounting_document_number,
    )
    if not open_items.exists():
        from django.http import Http404

        raise Http404("Open Item document not found.")
    reconciliation = next(
        iter(
            summarize_documents(
                dashboard_queryset().filter(
                    entity_code=entity_code,
                    accounting_document_number=accounting_document_number,
                )
            )
        ),
        None,
    )
    snapshots = ReviewDocumentSnapshot.objects.filter(
        entity_code=entity_code,
        accounting_document_number=accounting_document_number,
    ).select_related("completed_by").order_by("-calculation_version")
    has_current_snapshot = snapshots.filter(is_current=True).exists()
    blockers = (
        document_completion_blockers(open_items.first())
        if open_items.exists()
        else ["Invoice has no accounting lines."]
    )
    required_lines = [line for line in open_items if is_open_item_classifiable(line)]
    can_finalize = (
        bool(required_lines)
        and not has_current_snapshot
        and not blockers
        and all(
            line.status in (OpenItem.Status.COMPLETED, OpenItem.Status.REVIEWED)
            for line in required_lines
        )
    )
    return render(
        request,
        "transactions/open_item_document.html",
        {
            "entity_code": entity_code,
            "accounting_document_number": accounting_document_number,
            "open_items": open_items,
            "vendor_item": open_items.exclude(vendor_number="").first(),
            "reconciliation": reconciliation,
            "reconciliation_status_label": (
                reconciliation_status_label(reconciliation.reconciliation_status)
                if reconciliation is not None
                else ""
            ),
            "snapshots": snapshots,
            "can_reopen_invoice": has_current_snapshot
            and has_role(request.user, "Administrator"),
            "can_finalize_invoice": can_finalize
            and has_role(request.user, "Transaction User", "Administrator"),
            "completion_blockers": blockers,
            "invoice_anchor": open_items.first(),
        },
    )


@role_required("Transaction User", "Administrator")
def dashboard(request: HttpRequest) -> HttpResponse:
    blank_filters = {key: "" for key in _invoice_list_filters(request)}
    cards = [
        ("Active Invoices", len(_active_invoice_rows(blank_filters)), reverse("open-item-list")),
        ("Completed Invoices", completed_review_snapshots().count(), reverse("review-completed-items")),
    ]
    if has_role(request.user, "Administrator"):
        cards.append(
            ("Administrator Exceptions", len(build_exception_queue()), reverse("exceptions-queue"))
        )
    return render(
        request,
        "transactions/dashboard.html",
        {
            "cards": cards,
        },
    )


@role_required("Transaction User", "Administrator")
def open_item_detail(request: HttpRequest, pk: int) -> HttpResponse:
    open_item = get_object_or_404(OpenItem, pk=pk)
    if request.method == "POST" and not open_item.is_classifiable:
        messages.error(
            request,
            "This line is not a non-zero posting on an eligible G/L account.",
        )
        return redirect("open-item-detail", pk=pk)
    material_match = get_stored_material_match(open_item)
    matched_material = (
        material_match.candidates[0]
        if material_match.status == "MATCHED"
        else None
    )
    cost_description = matched_material.short_text if matched_material else ""
    current_decision = (
        open_item.classification_decisions.filter(is_current=True)
        .select_related("final_cost_type")
        .first()
    )
    latest_wht = open_item.wht_determinations.first()
    if request.method == "POST":
        form = ClassificationDecisionForm(request.POST)
        if form.is_valid():
            suggested_cost_type = _signed_recommendation_cost_type(
                form.cleaned_data["recommendation_signature"],
                open_item,
            )
            save_final_classification(
                open_item=open_item,
                final_cost_type=form.cleaned_data["final_cost_type"],
                classification_reason=form.cleaned_data["classification_reason"],
                user=request.user,
                suggested_cost_type=suggested_cost_type,
            )
            return redirect("open-item-detail", pk=open_item.pk)
    else:
        ai_recommendation = None
        ai_recommendation_unavailable = False
        can_recommend = (
            settings.LLM_COST_TYPE_RECOMMENDATIONS_ENABLED
            and current_decision is None
            and open_item.status
            in (
                OpenItem.Status.NEW,
                OpenItem.Status.IN_CLASSIFICATION,
                OpenItem.Status.RETURNED,
            )
            and open_item.is_classifiable
            and has_role(request.user, "Transaction User", "Administrator")
        )
        if can_recommend:
            try:
                ai_recommendation = recommend_cost_types(
                    open_item,
                    matched_material,
                )
            except RecommendationUnavailable as error:
                logger.warning(
                    "AI Cost Type recommendation unavailable (%s).",
                    type(error).__name__,
                )
                ai_recommendation_unavailable = True
        recommended_cost_type = (
            ai_recommendation.recommendations[0].cost_type
            if ai_recommendation and ai_recommendation.recommendations
            else None
        )
        recommendation_signature = (
            signing.dumps(
                {
                    "open_item_id": open_item.pk,
                    "cost_type_code": recommended_cost_type.code,
                },
                salt=_COST_TYPE_RECOMMENDATION_SALT,
            )
            if recommended_cost_type is not None
            else ""
        )
        form = ClassificationDecisionForm(
            initial={
                "final_cost_type": (
                    current_decision.final_cost_type_id
                    if current_decision is not None
                    else (
                        recommended_cost_type.pk
                        if recommended_cost_type is not None
                        else None
                    )
                ),
                "recommendation_signature": recommendation_signature,
            }
        )
    if request.method == "POST":
        ai_recommendation = None
        ai_recommendation_unavailable = False
    return render(
        request,
        "transactions/open_item_detail.html",
        {
            "open_item": open_item,
            "material_match": material_match,
            "matched_material": matched_material,
            "cost_description": cost_description,
            "classification_form": form,
            "ai_recommendation": ai_recommendation,
            "ai_recommendation_unavailable": ai_recommendation_unavailable,
            "current_decision": current_decision,
            "decision_history": open_item.classification_decisions.select_related(
                "final_cost_type"
            ),
            "latest_wht": latest_wht,
            "latest_wht_status_label": (
                determination_status_label(latest_wht) if latest_wht else ""
            ),
            "audit_events": open_item.audit_events.select_related("performed_by"),
            "exception_records": open_item.exception_records.select_related(
                "created_by", "resolved_by"
            ),
            "line_flag_form": LineFlagForm(),
            "line_exclusion_form": LineExclusionForm(),
            "can_classify": open_item.is_classifiable and has_role(request.user, "Transaction User", "Administrator"),
            "can_determine_wht": open_item.is_classifiable and has_role(request.user, "Transaction User", "Administrator"),
            "can_complete_classification": open_item.is_classifiable and has_role(request.user, "Transaction User", "Administrator"),
            "can_flag_line": has_role(request.user, "Transaction User", "Administrator"),
            "can_exclude_line": has_role(request.user, "Administrator"),
            "has_open_line_flag": open_item.exception_records.filter(
                issue_type=LineExceptionRecord.Type.USER_FLAG,
                status=LineExceptionRecord.Status.OPEN,
            ).exists(),
            "has_active_line_exclusion": open_item.exception_records.filter(
                status=LineExceptionRecord.Status.EXCLUDED,
            ).exists(),
            "has_completed_snapshot": ReviewDocumentSnapshot.objects.filter(
                entity_code=open_item.entity_code,
                accounting_document_number=open_item.accounting_document_number,
                is_current=True,
            ).exists(),
            "can_reopen_line": has_role(request.user, "Administrator")
            and ReviewDocumentSnapshot.objects.filter(
                entity_code=open_item.entity_code,
                accounting_document_number=open_item.accounting_document_number,
                is_current=True,
                review_status=ReviewDocumentSnapshot.ReviewStatus.REVIEWED,
            ).exists()
            and open_item.is_classifiable,
            "completion_blockers": document_completion_blockers(open_item),
        },
    )


@role_required("Administrator")
def reopen_line_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("open-item-detail", pk=pk)
    open_item = get_object_or_404(OpenItem, pk=pk)
    try:
        reopen_line(open_item, request.user)
    except (ValueError, PermissionError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Accounting line reopened for reclassification and review.")
    return redirect("open-item-detail", pk=pk)


@role_required("Administrator")
def reopen_invoice_view(request: HttpRequest, pk: int) -> HttpResponse:
    open_item = get_object_or_404(OpenItem, pk=pk)
    if request.method != "POST":
        return redirect(
            "open-item-document",
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
    try:
        reopen_invoice(open_item, request.user)
    except (ValueError, PermissionError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Invoice reopened for reclassification and review.")
    return redirect(
        "open-item-document",
        entity_code=open_item.entity_code,
        accounting_document_number=open_item.accounting_document_number,
    )


@role_required("Transaction User", "Administrator")
def flag_line_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("open-item-detail", pk=pk)
    open_item = get_object_or_404(OpenItem, pk=pk)
    form = LineFlagForm(request.POST)
    if form.is_valid():
        try:
            flag_line_for_review(open_item, request.user, form.cleaned_data["reason"])
        except (ValueError, PermissionError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Line flagged for Administrator review.")
    return redirect("open-item-detail", pk=pk)


@role_required("Administrator")
def exceptions_queue(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "transactions/exceptions_queue.html",
        {"entries": build_exception_queue()},
    )


@role_required("Administrator")
def resolve_line_flag_view(
    request: HttpRequest,
    record_id: int,
) -> HttpResponse:
    if request.method != "POST":
        return redirect("exceptions-queue")
    record = get_object_or_404(LineExceptionRecord, pk=record_id)
    form = LineFlagResolutionForm(request.POST)
    if form.is_valid():
        try:
            resolve_line_flag(
                record,
                request.user,
                form.cleaned_data["decision"],
                exclude_line=form.cleaned_data["action"] == "EXCLUDE",
            )
        except (ValueError, PermissionError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Administrator review flag resolved.")
    return redirect("exceptions-queue")


@role_required("Administrator")
def exclude_line_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("open-item-detail", pk=pk)
    open_item = get_object_or_404(OpenItem, pk=pk)
    form = LineExclusionForm(request.POST)
    if form.is_valid():
        try:
            exclude_line(open_item, request.user, form.cleaned_data["reason"])
        except (ValueError, PermissionError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Accounting line excluded.")
    return redirect("open-item-detail", pk=pk)


@role_required("Administrator")
def resolve_wht_exception_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("exceptions-queue")
    open_item = get_object_or_404(OpenItem, pk=pk)
    form = WHTExceptionResolutionForm(request.POST)
    if form.is_valid():
        treatment = form.cleaned_data["treatment"]
        try:
            resolve_wht_exception(
                open_item,
                request.user,
                form.cleaned_data["reason"],
                manual_rate=(
                    form.cleaned_data["manual_rate"] if treatment == "RATE" else None
                ),
                no_wht=treatment == "NO_WHT",
            )
        except (ValueError, PermissionError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "WHT exception resolved.")
    return redirect("open-item-detail", pk=pk)


@role_required("Administrator")
def recalculate_wht_exception_view(
    request: HttpRequest,
    pk: int,
) -> HttpResponse:
    if request.method != "POST":
        return redirect("exceptions-queue")
    open_item = get_object_or_404(OpenItem, pk=pk)
    try:
        determination = recalculate_wht_exception(open_item, request.user)
    except (ValueError, PermissionError) as error:
        messages.error(request, str(error))
    else:
        if determination.status in ("MATCHED", "NO_WHT"):
            messages.success(request, "WHT determination recalculated.")
        else:
            messages.warning(
                request,
                "Recalculation completed, but the WHT exception remains unresolved.",
            )
    return redirect("open-item-detail", pk=pk)


@role_required("Transaction User", "Administrator")
def complete_classification_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("open-item-detail", pk=pk)
    open_item = get_object_or_404(OpenItem, pk=pk)
    try:
        complete_classification(open_item, request.user)
        messages.success(request, "Classification completed.")
    except ValueError as error:
        messages.error(request, str(error))
    return redirect("open-item-detail", pk=pk)


def _retired_review_action(request: HttpRequest, pk: int) -> HttpResponse:
    open_item = get_object_or_404(OpenItem, pk=pk)
    messages.info(
        request,
        "Line-level review is no longer used. Finalize the invoice from its document summary.",
    )
    return redirect(
        "open-item-document",
        entity_code=open_item.entity_code,
        accounting_document_number=open_item.accounting_document_number,
    )


@role_required("Transaction User", "Administrator")
def finalize_invoice_view(request: HttpRequest, pk: int) -> HttpResponse:
    open_item = get_object_or_404(OpenItem, pk=pk)
    if request.method != "POST":
        return redirect(
            "open-item-document",
            entity_code=open_item.entity_code,
            accounting_document_number=open_item.accounting_document_number,
        )
    try:
        snapshot = finalize_invoice(open_item, request.user)
    except (ValueError, PermissionError) as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            f"Invoice finalized as snapshot version {snapshot.calculation_version}.",
        )
    return redirect(
        "open-item-document",
        entity_code=open_item.entity_code,
        accounting_document_number=open_item.accounting_document_number,
    )


@role_required("Transaction User", "Administrator")
def determine_wht_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("open-item-detail", pk=pk)
    open_item = get_object_or_404(OpenItem, pk=pk)
    if not open_item.is_classifiable:
        messages.error(request, "WHT can only be determined for non-zero postings on eligible G/L accounts.")
        return redirect("open-item-detail", pk=pk)
    decision = open_item.classification_decisions.filter(is_current=True).first()
    if decision is None:
        messages.error(request, "Select a Final Cost Type before determining WHT.")
    else:
        try:
            determine_wht(open_item, decision, user=request.user)
        except (ValueError, PermissionError) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "WHT determination calculated and saved.")
    return redirect("open-item-detail", pk=pk)


@role_required("Transaction User", "Administrator")
def submit_for_review_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _retired_review_action(request, pk)


@role_required("Transaction User", "Administrator")
def start_review_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _retired_review_action(request, pk)


@role_required("Transaction User", "Administrator")
def complete_review_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _retired_review_action(request, pk)


@role_required("Transaction User", "Administrator")
def return_item_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _retired_review_action(request, pk)


@role_required("Administrator")
def import_center(request: HttpRequest) -> HttpResponse:
    cleanup_errors = cleanup_expired_uploads()
    if cleanup_errors:
        messages.warning(
            request,
            "Some expired upload files could not be removed. The issue was logged "
            "for an administrator.",
        )
    if request.method == "POST":
        form = ImportCenterForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                session = preview_and_stage(
                    request.user,
                    form.cleaned_data["material_data_file"],
                    form.cleaned_data["open_items_file"],
                )
            except ValidationError as error:
                form.add_error(None, error)
            except (DatabaseError, OSError):
                logger.exception("Import Center preview staging failed.")
                form.add_error(
                    None,
                    "The files could not be staged privately for preview. "
                    "Please try again or contact an administrator.",
                )
            else:
                return redirect("import-preview", token=session.token)
    else:
        form = ImportCenterForm()

    batches = (
        ImportBatch.objects.filter(
            import_type__in=[
                ImportBatch.ImportType.MATERIAL_DATA,
                ImportBatch.ImportType.OPEN_ITEMS,
            ]
        )
        .select_related("uploaded_by")
        .order_by("-started_at", "-id")[:50]
    )
    return render(
        request,
        "transactions/import_center.html",
        {
            "form": form,
            "batches": batches,
            "max_import_file_size_mb": (
                settings.WHT_IMPORT_MAX_FILE_SIZE + 1024 * 1024 - 1
            ) // (1024 * 1024),
        },
    )


@role_required("Administrator")
def import_preview(request: HttpRequest, token) -> HttpResponse:
    session = get_object_or_404(
        ImportUploadSession,
        token=token,
        uploaded_by=request.user,
    )
    if request.method == "POST":
        try:
            session, cleanup_errors = confirm_import(session, request.user)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
            return redirect("import-preview", token=session.token)
        except (CommandError, OSError, DatabaseError):
            logger.exception("Import Center confirmation failed.")
            messages.error(
                request,
                "The import failed and database changes were rolled back. "
                "Check the files and retry, or contact an administrator.",
            )
            return redirect("import-preview", token=session.token)
        rejected_rows = sum(
            batch.rows_rejected
            for batch in session.import_batches.all()
        )
        if cleanup_errors:
            messages.warning(
                request,
                "The import completed, but staged files could not all be removed. "
                "The cleanup failure was logged.",
            )
        if rejected_rows:
            messages.warning(
                request,
                f"The import completed with {rejected_rows} rejected row(s). "
                "Review the batch details for row-level errors.",
            )
        if not cleanup_errors and not rejected_rows:
            messages.success(request, "Import completed successfully.")
        return redirect("import-preview", token=session.token)

    cleanup_errors = cleanup_expired_uploads()
    if cleanup_errors:
        messages.warning(
            request,
            "Some staged upload files could not be removed. The cleanup failure "
            "was logged for an administrator.",
        )
    session.refresh_from_db()
    batches = session.import_batches.select_related("uploaded_by").prefetch_related(
        "validation_errors"
    )
    return render(
        request,
        "transactions/import_preview.html",
        {"upload_session": session, "batches": batches},
    )


@role_required("Administrator")
def import_batch_detail(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(
        ImportBatch.objects.filter(
            import_type__in=[
                ImportBatch.ImportType.MATERIAL_DATA,
                ImportBatch.ImportType.OPEN_ITEMS,
            ]
        )
        .select_related("uploaded_by", "upload_session")
        .prefetch_related("validation_errors"),
        pk=pk,
    )
    return render(
        request,
        "transactions/import_batch_detail.html",
        {
            "batch": batch,
            "material_match_counts": (
                get_batch_match_counts(batch)
                if batch.import_type == ImportBatch.ImportType.OPEN_ITEMS
                else None
            ),
        },
    )
