from datetime import datetime
from io import BytesIO
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from django.utils import timezone


HEADERS = [
    "Entity",
    "Accounting Document Number",
    "Supplier ID",
    "Supplier Name",
    "Supplier Country",
    "Document Date",
    "Posting Date",
    "Document Currency",
    "Invoice Amount",
    "Workflow Status",
    "Lines Requiring Work",
    "Outstanding Exceptions",
    "Calculated WHT",
    "Signed WHT Posted",
    "Difference",
    "Reconciliation Status",
    "Completion / Exception Status",
    "Completed At",
]

MONEY_COLUMNS = {8, 12, 13, 14}


def build_invoice_workbook(
    rows: Iterable[dict[str, object]],
    filters: dict[str, str],
    *,
    sheet_title: str,
) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="17324D")
    for row in rows:
        completed_at = row["completed_at"]
        if completed_at is not None and completed_at.tzinfo is not None:
            completed_at = timezone.localtime(completed_at).replace(tzinfo=None)
        sheet.append(
            [
                row["entity_code"],
                row["accounting_document_number"],
                row["supplier"],
                row["vendor_name"],
                row["country"],
                row["document_date"],
                row["posting_date"],
                row["document_currency"],
                row["document_amount"],
                row["workflow_status"],
                row["lines_requiring_work"],
                row["outstanding_exception_count"],
                row["calculated_wht"],
                row["posted_wht"],
                row["difference"],
                row["reconciliation_status"],
                row["completion_status"],
                completed_at,
            ]
        )
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if cell.column in MONEY_COLUMNS:
                cell.number_format = '#,##0.00;[Red](#,##0.00);0.00'
        row[17].number_format = "yyyy-mm-dd hh:mm"
    for index, header in enumerate(HEADERS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = max(
            14,
            min(34, len(header) + 2),
        )
    info = workbook.create_sheet("Export Info")
    info.append(["Export timestamp", datetime.now().astimezone().isoformat()])
    info.append(["Filter", "Value"])
    for key, value in filters.items():
        if value:
            info.append([key, value])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def build_workbook(rows, filters: dict[str, str]) -> BytesIO:
    return build_invoice_workbook(rows, filters, sheet_title="Active Invoices")
