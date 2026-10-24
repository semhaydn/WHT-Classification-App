from io import BytesIO
from typing import Iterable

from transactions.models import ReviewDocumentSnapshot
from transactions.services.open_items_export import build_invoice_workbook
from transactions.services.review_snapshots import serialize_review_snapshot


def _row(snapshot: ReviewDocumentSnapshot) -> dict[str, object]:
    values = serialize_review_snapshot(snapshot)
    return {
        "entity_code": values["entity_code"],
        "accounting_document_number": values["accounting_document_number"],
        "supplier": values["supplier"],
        "vendor_name": values["vendor_name"],
        "country": values["country"],
        "document_date": None,
        "posting_date": None,
        "document_currency": values["document_currency"],
        "document_amount": values["document_amount"],
        "workflow_status": "Completed",
        "lines_requiring_work": 0,
        "outstanding_exception_count": int(
            bool(values["mapping_required"] or values["exception_status"] != "Resolved")
        ),
        "calculated_wht": values["calculated_wht"],
        "posted_wht": values["booked_wht"],
        "difference": values["modification_required"],
        "reconciliation_status": values["reconciliation_status"],
        "completion_status": values["exception_status"],
        "completed_at": values["completed_at"],
    }


def build_completed_review_workbook(
    snapshots: Iterable[ReviewDocumentSnapshot],
    filters: dict[str, str],
) -> BytesIO:
    return build_invoice_workbook(
        (_row(snapshot) for snapshot in snapshots),
        filters,
        sheet_title="Completed Invoices",
    )
