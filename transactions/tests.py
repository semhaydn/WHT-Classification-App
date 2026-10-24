import csv
import sqlite3
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO, StringIO

import pytest
from openpyxl import load_workbook
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.test import override_settings

from reference_data.models import CostType, SupplierCountry, WHTMapping
from .models import (
    AuditEvent,
    ClassificationDecision,
    ImportBatch,
    ImportUploadSession,
    GLEligibility,
    LineExceptionRecord,
    MaterialData,
    MaterialMatch,
    OpenItem,
    ReviewAction,
    ReviewDocumentSnapshot,
    ReviewLineCalculationSnapshot,
)
from .services.reconciliation import (
    determination_status_label,
    has_valid_determination,
)
from .models import WHTDetermination
from .services.wht_determination import determine_wht, match_wht
from .services.material_matching import match_material
from reference_data.services import approve_wht_rule
from .services.review import (
    complete_review,
    finalize_invoice,
    return_item,
    start_review,
    submit_for_review,
)
from .services.classification import complete_classification, save_final_classification
from .services.open_items_dashboard import dashboard_queryset, summarize_documents
from .services.open_items_export import HEADERS
from .services.review_snapshots import (
    completed_review_snapshots,
    create_review_document_snapshot,
    serialize_review_snapshot,
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
from .services.invoice_amounts import invoice_amount_from_control_lines


def classifiable_item() -> OpenItem:
    return OpenItem.objects.filter(
        gl_account="210020",
        document_currency_amount__gt=0,
    ).first()


def document_supplier_line(item: OpenItem) -> OpenItem:
    return OpenItem.objects.exclude(vendor_number="").get(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    )


def transaction_user_client(
    username: str = "transaction-user",
    *,
    is_staff: bool = False,
    **defaults,
) -> Client:
    user = User.objects.create_user(username=username, is_staff=is_staff)
    group, _ = Group.objects.get_or_create(name="Transaction User")
    user.groups.add(group)
    client = Client(**defaults)
    client.force_login(user)
    return client


def create_approved_wht_mapping(**values) -> WHTMapping:
    approver, _ = User.objects.get_or_create(
        username="wht-rule-administrator",
        defaults={"is_staff": True},
    )
    approver.is_staff = True
    approver.save(update_fields=["is_staff"])
    administrator_group, _ = Group.objects.get_or_create(name="Administrator")
    approver.groups.add(administrator_group)
    values.setdefault("version", 1)
    values.setdefault("effective_from", date(1900, 1, 1))
    values.setdefault("status", WHTMapping.Status.APPROVED)
    values.setdefault("approved_by", approver)
    values.setdefault("approved_at", timezone.now())
    return WHTMapping.objects.create(**values)


def create_completed_snapshot_test_document(
    document_number: str,
    *,
    invoice_amount: Decimal,
    expense_amount: Decimal,
    rate: Decimal | None,
    booked_amounts: tuple[Decimal, ...] = (),
    country: str = "GB",
    expense_currency: str = "GBP",
) -> ReviewDocumentSnapshot:
    call_command("import_open_items")
    CostType.objects.get_or_create(code="RENTAL", defaults={"name": "RENTAL"})
    invoice = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="708000226",
        gl_account="210000",
    )
    expense = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="708000226",
        gl_account="210020",
    )
    invoice.accounting_document_number = document_number
    invoice.document_currency_amount = -invoice_amount
    invoice.document_currency = "GBP"
    invoice.save()
    expense.accounting_document_number = document_number
    expense.document_currency_amount = expense_amount
    expense.document_currency = expense_currency
    expense.save()

    SupplierCountry.objects.update_or_create(
        supplier_id=invoice.vendor_number,
        defaults={
            "country_code": country,
            "supplier_name": invoice.vendor_name,
        },
    )
    cost_type = CostType.objects.get(code="RENTAL")
    decision = ClassificationDecision.objects.create(
        open_item=expense,
        final_cost_type=cost_type,
        classification_reason="Synthetic completed-snapshot acceptance test",
    )
    if rate is not None:
        create_approved_wht_mapping(
            supplier=invoice.vendor_number,
            supplier_name=invoice.vendor_name,
            cost_category=cost_type,
            wht_rate=rate,
            country_code=country,
            source_title=f"Synthetic test rule for {document_number}",
        )
    determine_wht(expense, decision)
    expense.status = OpenItem.Status.REVIEWED
    expense.save(update_fields=["status"])

    completion_action = ReviewAction.objects.create(
        open_item=expense,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
    )
    for line_number, amount in enumerate(booked_amounts, start=3):
        posting = OpenItem.objects.get(
            entity_code="SYN01",
            accounting_document_number=document_number,
            gl_account="210000",
        )
        posting.pk = None
        posting.id = None
        posting.accounting_document_number = document_number
        posting.line_number = str(line_number)
        posting.gl_account = "229220"
        posting.gl_account_description = "Withholding tax"
        posting.document_currency_amount = amount
        posting.status = OpenItem.Status.NEW
        posting.save()

    return create_review_document_snapshot(
        expense,
        completion_action=completion_action,
    )


def open_item_csv_row(item: OpenItem) -> dict[str, str]:
    def date_value(value) -> str:
        return value.isoformat() if value else ""

    return {
        "Title": item.title,
        "Entity": item.entity_code,
        "Document Number": item.accounting_document_number,
        "Line Number": item.line_number,
        "Document Type": item.document_type,
        "Document Date": date_value(item.document_date),
        "Posting Date": date_value(item.posting_date),
        "Document Reference": item.document_reference,
        "Vendor Number": item.vendor_number,
        "Vendor Name": item.vendor_name,
        "Purchasing Document": item.purchasing_document,
        "Purchasing Document Item": item.purchasing_document_item,
        "MaterialData Key": item.material_data_key,
        "Material Group": item.material_group,
        "Material": item.material,
        "Material Description": item.material_description,
        "Document Currency Amount": str(item.document_currency_amount),
        "Document Currency": item.document_currency,
        "Company Code Currency Amount": str(item.company_code_currency_amount),
        "Company Code Currency": item.company_code_currency,
        "Group Currency Amount": str(item.group_currency_amount),
        "Group Currency": item.group_currency,
        "GL Account": item.gl_account,
        "GL Account Description": item.gl_account_description,
        "Offsetting Account": item.offsetting_account,
        "Text": item.text,
        "Payment Block": item.payment_block,
        "Payment Method": item.payment_method,
        "Assignment": item.assignment,
        "Net Due Date": date_value(item.net_due_date),
        "Clearing Date": date_value(item.clearing_date),
        "Payment Date": date_value(item.payment_date),
        "Clearing Document": item.clearing_document,
        "Comments": item.comments,
        "Country": item.country,
    }


@pytest.mark.django_db
def test_import_creates_open_items_with_decimal_amounts() -> None:
    call_command("import_open_items")

    assert OpenItem.objects.count() == 42
    item = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="708000231",
        line_number="1",
    )
    assert item.document_currency_amount == Decimal("-1583.66")


@pytest.mark.django_db
def test_reimport_is_idempotent() -> None:
    call_command("import_open_items")
    call_command("import_open_items")

    assert OpenItem.objects.count() == 42


@pytest.mark.django_db
def test_material_data_reimport_is_idempotent() -> None:
    call_command("import_material_data")
    first_count = MaterialData.objects.count()

    call_command("import_material_data")

    assert first_count > 0
    assert MaterialData.objects.count() == first_count


@pytest.mark.django_db
def test_historical_cost_type_import_is_idempotent_and_suggestion_only() -> None:
    call_command("import_cost_type_decisions")
    from .models import HistoricalCostTypeSuggestion

    first_count = HistoricalCostTypeSuggestion.objects.count()
    call_command("import_cost_type_decisions")

    assert first_count > 0
    assert HistoricalCostTypeSuggestion.objects.count() == first_count
    assert ClassificationDecision.objects.count() == 0


@pytest.mark.django_db
def test_open_item_list_and_detail_pages() -> None:
    call_command("import_open_items")
    client = transaction_user_client()

    list_response = client.get(reverse("open-item-list"))
    detail_response = client.get(reverse("open-item-detail", args=[OpenItem.objects.first().pk]))

    assert list_response.status_code == 200
    assert detail_response.status_code == 200


@pytest.mark.django_db
def test_line_detail_shows_matched_material_cost_description() -> None:
    call_command("import_open_items")
    call_command("import_material_data")
    item = OpenItem.objects.get(
        accounting_document_number="708000230",
        line_number="2",
    )
    material = MaterialData.objects.get(
        purchasing_document=item.purchasing_document,
        item=item.purchasing_document_item,
    )

    response = transaction_user_client().get(
        reverse("open-item-detail", args=[item.pk])
    )

    assert response.status_code == 200
    assert response.context["matched_material"] == material
    assert response.context["cost_description"] == material.short_text
    assert material.short_text in response.content.decode()
    assert "Cost Description from MaterialData" in response.content.decode()


@pytest.mark.django_db
def test_open_item_list_groups_lines_by_document() -> None:
    call_command("import_open_items")
    client = transaction_user_client()

    response = client.get(reverse("open-item-list"))

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 19
    assert response.context["open_items"].count() == 19

    document = next(
        document
        for document in response.context["page_obj"].object_list
        if document["accounting_document_number"] == "708000231"
    )
    assert document["invoice_amount"] == Decimal("1583.66")
    assert document["invoice_currency"] == "GBP"
    assert "1583.66" in response.content.decode()


@pytest.mark.django_db
def test_retired_ap_queue_filter_redirects_to_active_invoice_list() -> None:
    call_command("import_open_items")
    completed = OpenItem.objects.first()
    completed.status = OpenItem.Status.COMPLETED
    completed.save(update_fields=["status"])
    reviewed = OpenItem.objects.last()
    reviewed.status = OpenItem.Status.REVIEWED
    reviewed.save(update_fields=["status"])

    response = transaction_user_client().get(
        reverse("open-item-list"),
        {"queue": "ap_visible"},
    )

    assert response.status_code == 302
    assert response.url == reverse("open-item-list")


@pytest.mark.django_db
def test_document_page_lists_lines_and_links_to_line_detail() -> None:
    call_command("import_open_items")
    client = transaction_user_client()

    response = client.get(
        reverse(
            "open-item-document",
            args=["SYN01", "708000230"],
        )
    )

    assert response.status_code == 200
    assert response.context["open_items"].count() == 2
    assert f'href="{reverse("open-item-detail", args=[response.context["open_items"].first().pk])}"' in response.content.decode()


@pytest.mark.django_db
def test_open_item_views_show_vendor_details_and_classifiable_scope() -> None:
    call_command("import_open_items")
    client = transaction_user_client()

    list_response = client.get(reverse("open-item-list"))
    document_response = client.get(
        reverse("open-item-document", args=["SYN01", "708000231"])
    )
    invoice_line = OpenItem.objects.get(
        accounting_document_number="708000231",
        line_number="1",
    )
    expense_line = OpenItem.objects.get(
        accounting_document_number="708000231",
        line_number="2",
    )
    invoice_response = client.get(
        reverse("open-item-detail", args=[invoice_line.pk])
    )

    assert "VEN100185" in list_response.content.decode()
    assert "Synthetic Supplier 1819" in document_response.content.decode()
    assert expense_line.is_classifiable is True
    assert invoice_line.is_classifiable is False
    assert "Not a classifiable expense line" in invoice_response.content.decode()


@pytest.mark.django_db
def test_open_items_export_contains_dashboard_columns_and_export_info() -> None:
    call_command("import_open_items")
    client = transaction_user_client(HTTP_HOST="127.0.0.1")
    response = client.get(reverse("open-item-export"))

    assert response.status_code == 200
    workbook = load_workbook(filename=BytesIO(response.content), data_only=False)
    assert workbook["Active Invoices"].max_column == len(HEADERS)
    assert workbook["Active Invoices"].freeze_panes == "A2"
    assert workbook["Export Info"]["A1"].value == "Export timestamp"


@pytest.mark.django_db
def test_invoice_amount_sign_is_shared_by_active_completed_lists_and_exports() -> None:
    call_command("import_open_items")
    expected = {
        "708000226": (Decimal("-933.26"), Decimal("933.26")),
        "708000230": (Decimal("513.61"), Decimal("-513.61")),
        "708000247": (Decimal("-17718.04"), Decimal("17718.04")),
    }
    client = transaction_user_client()
    active_response = client.get(reverse("open-item-list"))
    active_export = load_workbook(
        filename=BytesIO(client.get(reverse("open-item-export")).content),
        data_only=True,
    )["Active Invoices"]
    active_rows = {
        row["accounting_document_number"]: row
        for row in active_response.context["page_obj"].object_list
    }
    active_export_rows = {
        str(row[1]): row[8]
        for row in active_export.iter_rows(min_row=2, values_only=True)
    }
    for document, (control_amount, invoice_amount) in expected.items():
        control_lines = list(
            OpenItem.objects.filter(
                accounting_document_number=document,
                gl_account="210000",
            )
        )
        assert sum(
            (line.document_currency_amount for line in control_lines),
            Decimal("0.00"),
        ) == control_amount
        assert invoice_amount_from_control_lines(control_lines) == invoice_amount
        assert active_rows[document]["invoice_amount"] == invoice_amount
        assert active_export_rows[document] == float(invoice_amount)

        lines = list(
            OpenItem.objects.filter(accounting_document_number=document).order_by("pk")
        )
        eligible_lines = [line for line in lines if line.is_classifiable]
        for line in eligible_lines:
            line.status = OpenItem.Status.REVIEWED
            line.save(update_fields=["status"])
        completion_item = eligible_lines[0] if eligible_lines else control_lines[0]
        completion_action = ReviewAction.objects.create(
            open_item=completion_item,
            action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
        )
        snapshot = create_review_document_snapshot(
            completion_item,
            completion_action=completion_action,
        )
        assert snapshot.document_amount == invoice_amount

    completed_response = client.get(reverse("review-completed-items"))
    completed_rows = {
        row["accounting_document_number"]: row
        for row in completed_response.context["page_obj"].object_list
    }
    completed_export = load_workbook(
        filename=BytesIO(client.get(reverse("review-completed-export")).content),
        data_only=True,
    )["Completed Invoices"]
    completed_export_rows = {
        str(row[1]): row[8]
        for row in completed_export.iter_rows(min_row=2, values_only=True)
    }
    for document, (_, invoice_amount) in expected.items():
        assert completed_rows[document]["document_amount"] == invoice_amount
        assert completed_export_rows[document] == float(invoice_amount)


@pytest.mark.parametrize(
    ("control_amounts", "expected_invoice_amount"),
    [
        ((Decimal("513.61"),), Decimal("-513.61")),
        ((Decimal("-933.26"),), Decimal("933.26")),
        ((Decimal("400.00"), Decimal("113.61")), Decimal("-513.61")),
        ((Decimal("-1000.00"), Decimal("66.74")), Decimal("933.26")),
    ],
)
def test_invoice_amount_helper_negates_sum_of_synthetic_control_lines(
    control_amounts: tuple[Decimal, ...],
    expected_invoice_amount: Decimal,
) -> None:
    control_lines = [
        OpenItem(
            gl_account="210000",
            document_currency="USD",
            document_currency_amount=amount,
        )
        for amount in control_amounts
    ]
    control_lines.append(
        OpenItem(
            gl_account="229220",
            document_currency="USD",
            document_currency_amount=Decimal("-18.62"),
        )
    )

    assert invoice_amount_from_control_lines(control_lines) == expected_invoice_amount


@pytest.mark.django_db
def test_dashboard_counts_and_filter_links() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier_line.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier_line.vendor_name,
        },
    )
    item.status = OpenItem.Status.REVIEWED
    item.save(update_fields=["status"])

    client = transaction_user_client()
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    cards = {label: (count, url) for label, count, url in response.context["cards"]}
    assert set(cards) == {"Active Invoices", "Completed Invoices"}
    assert cards["Active Invoices"][0] > 0
    assert cards["Completed Invoices"][0] == 0


@pytest.mark.django_db
def test_dashboard_shows_only_invoice_focused_navigation_to_transaction_users() -> None:
    call_command("import_open_items")
    user = User.objects.create_user(username="dashboard-reviewer", password="test-password")
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name="Reviewer")
    user.groups.add(group)
    client = Client()
    client.force_login(user)
    response = client.get(reverse("dashboard"))

    cards = {label: (count, url) for label, count, url in response.context["cards"]}
    assert set(cards) == {"Active Invoices", "Completed Invoices"}
    assert b"Needs Classification" not in response.content
    assert b"Review Completed Items" not in response.content
    assert b"Administrator Exceptions" not in response.content


@pytest.mark.django_db
def test_open_item_filters_by_entity_vendor_status_and_document() -> None:
    call_command("import_open_items")
    client = transaction_user_client()

    response = client.get(
        reverse("open-item-list"),
        {
            "entity": "SYN01",
            "vendor": "VEN100926",
            "status": "NEW",
            "document": "708000230",
        },
    )

    assert response.status_code == 200
    assert response.context["page_obj"].paginator.count == 1
    assert response.context["page_obj"].object_list[0]["accounting_document_number"] == "708000230"


@pytest.mark.django_db
def test_invalid_row_is_reported(tmp_path) -> None:
    _, _, headers, row = import_center_files()
    csv_file = write_csv_file(
        tmp_path,
        "open_items.csv",
        headers,
        [{**row, "Posting Date": "not-a-date"}],
    )

    call_command("import_open_items", str(csv_file))
    batch = ImportBatch.objects.get(import_type=ImportBatch.ImportType.OPEN_ITEMS)
    assert batch.rows_rejected == 1
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_open_items_import_rejects_the_whole_file_when_a_row_is_invalid(
    tmp_path,
) -> None:
    _, _, headers, valid_row = import_center_files()
    invalid_row = {**valid_row, "Document Number": "900000002", "Posting Date": "bad"}
    csv_file = write_csv_file(
        tmp_path,
        "mixed-validity.csv",
        headers,
        [valid_row, invalid_row],
    )

    call_command("import_open_items", str(csv_file))

    batch = ImportBatch.objects.get(import_type=ImportBatch.ImportType.OPEN_ITEMS)
    assert batch.rows_read == 2
    assert batch.rows_created == 0
    assert batch.rows_rejected == 1
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_open_items_import_rejects_all_duplicate_business_key_rows(tmp_path) -> None:
    _, _, headers, row = import_center_files()
    csv_file = write_csv_file(tmp_path, "duplicate-lines.csv", headers, [row, row])

    call_command("import_open_items", str(csv_file))

    batch = ImportBatch.objects.get(import_type=ImportBatch.ImportType.OPEN_ITEMS)
    assert batch.rows_created == 0
    assert batch.rows_rejected == 2
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_open_items_import_does_not_overwrite_conflicting_source_content(
    tmp_path,
) -> None:
    _, _, headers, row = import_center_files()
    initial_file = write_csv_file(tmp_path, "initial.csv", headers, [row])
    call_command("import_open_items", str(initial_file))
    item = OpenItem.objects.get(
        entity_code="SYN99",
        accounting_document_number="900000001",
        line_number="2",
    )
    item.status = OpenItem.Status.IN_CLASSIFICATION
    item.save(update_fields=["status"])
    changed_row = {**row, "Document Currency Amount": "200.00"}
    conflicting_file = write_csv_file(
        tmp_path,
        "conflict.csv",
        headers,
        [changed_row],
    )

    call_command("import_open_items", str(conflicting_file))

    item.refresh_from_db()
    conflict_batch = ImportBatch.objects.get(source_filename="conflict.csv")
    assert conflict_batch.rows_created == 0
    assert conflict_batch.rows_rejected == 1
    assert item.document_currency_amount == Decimal("100.00")
    assert item.status == OpenItem.Status.IN_CLASSIFICATION


@pytest.mark.django_db
def test_material_import_uses_last_occurrence_for_duplicate_business_keys(
    capsys,
) -> None:
    call_command("import_material_data")

    assert MaterialData.objects.count() == 224
    duplicate = MaterialData.objects.get(
        purchasing_document="4570000001",
        item="500",
    )
    assert duplicate.short_text == "Invoice-related service - synthetic description"
    output = capsys.readouterr()
    assert "rejected=0" in output.out
    assert "row " in output.err
    assert "is used" in output.err


@pytest.mark.django_db
def test_wht_mapping_import_rejects_valid_rows_when_any_row_is_invalid(
    tmp_path,
) -> None:
    call_command("import_reference_data")
    headers = (
        "Supplier",
        "Supplier Name",
        "Cost Category",
        "WHT %",
        "Country",
        "Title",
    )
    valid_row = {
        "Supplier": "SYNTHETIC-ATOMIC",
        "Supplier Name": "Synthetic Supplier",
        "Cost Category": "RENTAL",
        "WHT %": "0.10",
        "Country": "GB",
        "Title": "Synthetic import test",
    }
    invalid_row = {
        **valid_row,
        "Supplier": "SYNTHETIC-INVALID",
        "WHT %": "not-a-rate",
    }
    csv_file = write_csv_file(
        tmp_path,
        "mixed-wht-rules.csv",
        headers,
        [valid_row, invalid_row],
    )

    call_command("import_wht_mapping", str(csv_file))

    assert WHTMapping.objects.count() == 0


@pytest.mark.django_db
def test_material_matching_returns_not_found() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.get(accounting_document_number="708000231", line_number="1")

    result = match_material(item)

    assert result.status == "NOT_FOUND"
    assert result.candidate_count == 0
    stored_match = item.material_matches.get()
    assert stored_match.status == "NOT_FOUND"
    assert stored_match.candidate_count == 0
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.MATERIAL_MATCH_NOT_FOUND,
    ).exists()


@pytest.mark.django_db
def test_material_matching_returns_matched() -> None:
    call_command("import_open_items")
    call_command("import_material_data")
    item = OpenItem.objects.get(accounting_document_number="708000230", line_number="2")

    result = match_material(item)

    assert result.status == "MATCHED"
    assert result.candidate_count == 1
    assert item.material_matches.first().material_data == result.candidates[0]
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.MATERIAL_MATCHED,
        previous_value__status="NOT_FOUND",
    ).exists()


@pytest.mark.django_db
def test_invoice_and_line_detail_gets_do_not_refresh_material_match_state() -> None:
    call_command("import_open_items")
    call_command("import_material_data")
    item = OpenItem.objects.get(
        accounting_document_number="708000230",
        line_number="2",
    )
    match_record = item.material_matches.first()
    assert match_record is not None
    old_checked_at = timezone.now() - timedelta(days=1)
    MaterialMatch.objects.filter(pk=match_record.pk).update(
        last_checked_at=old_checked_at,
    )
    match_record.refresh_from_db()
    before = (
        match_record.status,
        match_record.candidate_count,
        match_record.material_data_id,
        match_record.last_checked_at,
        item.document_currency_amount,
        item.status,
    )
    audit_count = AuditEvent.objects.filter(open_item=item).count()
    client = transaction_user_client()

    invoice_response = client.get(
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        )
    )
    detail_response = client.get(reverse("open-item-detail", args=[item.pk]))

    assert invoice_response.status_code == detail_response.status_code == 200
    match_record.refresh_from_db()
    item.refresh_from_db()
    assert (
        match_record.status,
        match_record.candidate_count,
        match_record.material_data_id,
        match_record.last_checked_at,
        item.document_currency_amount,
        item.status,
    ) == before
    assert AuditEvent.objects.filter(open_item=item).count() == audit_count

    match_material(item)
    match_record.refresh_from_db()
    assert match_record.last_checked_at > old_checked_at


@pytest.mark.django_db
def test_material_matching_uses_the_last_imported_duplicate_source_key() -> None:
    call_command("import_open_items")
    call_command("import_material_data")
    item = OpenItem.objects.get(accounting_document_number="708000226", line_number="2")
    item.purchasing_document = "4570000001"
    item.purchasing_document_item = "500"
    item.save(update_fields=["purchasing_document", "purchasing_document_item"])

    result = match_material(item)

    assert result.status == "MATCHED"
    assert result.candidate_count == 1


@pytest.mark.django_db
def test_material_business_key_is_unique() -> None:
    call_command("import_material_data")
    material = MaterialData.objects.first()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MaterialData.objects.create(
                **{
                    field.name: getattr(material, field.name)
                    for field in MaterialData._meta.fields
                    if field.name not in {"id", "created_at", "updated_at"}
                }
            )


@pytest.mark.django_db
def test_material_data_admin_is_limited_to_administrators() -> None:
    model_admin = admin.site._registry[MaterialData]
    classifier = User.objects.create_user(
        username="material-classifier",
        is_staff=True,
    )
    transaction_user = User.objects.create_user(
        username="material-transaction-user",
        is_staff=True,
    )
    transaction_group, _ = Group.objects.get_or_create(name="Transaction User")
    transaction_user.groups.add(transaction_group)
    administrator = User.objects.create_user(
        username="material-administrator",
        is_staff=True,
    )
    administrator_group, _ = Group.objects.get_or_create(name="Administrator")
    administrator.groups.add(administrator_group)
    factory = RequestFactory()
    classifier_request = factory.get("/admin/")
    classifier_request.user = classifier
    transaction_request = factory.get("/admin/")
    transaction_request.user = transaction_user
    administrator_request = factory.get("/admin/")
    administrator_request.user = administrator

    assert not model_admin.has_add_permission(classifier_request)
    assert not model_admin.has_add_permission(transaction_request)
    assert model_admin.has_add_permission(administrator_request)


@pytest.mark.django_db
def test_gl_eligibility_admin_is_administrator_only_and_supports_management() -> None:
    model_admin = admin.site._registry[GLEligibility]
    transaction_client = transaction_user_client(
        username="gl-eligibility-transaction-user",
        is_staff=True,
    )
    transaction_user = User.objects.get(
        username="gl-eligibility-transaction-user"
    )
    transaction_request = RequestFactory().get("/admin/")
    transaction_request.user = transaction_user
    eligibility = GLEligibility.objects.create(
        gl_account="ADMIN-ONLY-GL",
        is_eligible=False,
    )

    assert not model_admin.has_module_permission(transaction_request)
    assert not model_admin.has_view_permission(transaction_request)
    assert not model_admin.has_add_permission(transaction_request)
    assert not model_admin.has_change_permission(transaction_request)
    assert not model_admin.has_delete_permission(transaction_request)

    for url in (
        reverse("admin:transactions_gleligibility_changelist"),
        reverse("admin:transactions_gleligibility_add"),
        reverse("admin:transactions_gleligibility_change", args=[eligibility.pk]),
        reverse("admin:transactions_gleligibility_delete", args=[eligibility.pk]),
    ):
        assert transaction_client.get(url).status_code == 403

    administrator = import_center_admin()
    administrator_request = RequestFactory().get("/admin/")
    administrator_request.user = administrator
    assert model_admin.has_module_permission(administrator_request)
    assert model_admin.has_view_permission(administrator_request)
    assert model_admin.has_add_permission(administrator_request)
    assert model_admin.has_change_permission(administrator_request)
    assert model_admin.has_delete_permission(administrator_request)

    admin_client = Client()
    admin_client.force_login(administrator)
    assert admin_client.post(
        reverse("admin:transactions_gleligibility_add"),
        {
            "gl_account": "ADMIN-MANAGED-GL",
            "is_eligible": "on",
            "description": "Managed by administrator",
        },
    ).status_code == 302
    managed = GLEligibility.objects.get(gl_account="ADMIN-MANAGED-GL")
    assert admin_client.post(
        reverse("admin:transactions_gleligibility_change", args=[managed.pk]),
        {
            "gl_account": "ADMIN-MANAGED-GL",
            "is_eligible": "",
            "description": "Explicitly excluded",
        },
    ).status_code == 302
    managed.refresh_from_db()
    assert managed.is_eligible is False
    assert admin_client.post(
        reverse("admin:transactions_gleligibility_delete", args=[managed.pk]),
        {"post": "yes"},
    ).status_code == 302
    assert not GLEligibility.objects.filter(pk=managed.pk).exists()


@pytest.mark.django_db
def test_saving_classification_keeps_history_and_audit_event() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    cost_type = CostType.objects.get(code="RENTAL")

    from .services.classification import save_final_classification

    save_final_classification(item, cost_type, "Reviewed against source description")

    assert ClassificationDecision.objects.filter(open_item=item).count() == 1
    assert ClassificationDecision.objects.get(open_item=item).is_current is True
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.CLASSIFICATION_SELECTED,
    ).count() == 1


@pytest.mark.django_db
def test_changing_classification_retains_previous_decision() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    from .services.classification import save_final_classification

    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Initial")
    save_final_classification(item, CostType.objects.get(code="LABOR"), "Corrected")

    decisions = ClassificationDecision.objects.filter(open_item=item)
    assert decisions.count() == 2
    assert decisions.filter(is_current=True).get().final_cost_type.code == "LABOR"
    assert decisions.filter(is_current=False).get().final_cost_type.code == "RENTAL"
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.CLASSIFICATION_CHANGED,
    ).count() == 1


@pytest.mark.django_db
def test_classification_form_saves_from_detail_page() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    from django.contrib.auth.models import Group

    user = User.objects.create_user(username="classifier", password="test-password")
    group, _ = Group.objects.get_or_create(name="Classifier")
    user.groups.add(group)
    client = pytest.importorskip("django.test").Client()
    assert client.login(username="classifier", password="test-password")
    response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": CostType.objects.get(code="RENTAL").pk,
            "classification_reason": "Reviewed by user",
        },
    )

    assert response.status_code == 302
    assert ClassificationDecision.objects.get(open_item=item).final_cost_type.code == "RENTAL"


@pytest.mark.django_db
def test_anonymous_classification_post_redirects_to_login() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    response = pytest.importorskip("django.test").Client().post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": CostType.objects.get(code="RENTAL").pk,
            "classification_reason": "Unauthorized attempt",
        },
    )

    assert response.status_code == 302
    assert response.url.startswith("/login/?next=")
    assert not ClassificationDecision.objects.filter(open_item=item).exists()


@pytest.mark.django_db
def test_role_permissions_restrict_browser_workflow_actions() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    classifier = User.objects.create_user(username="role-classifier", password="password")
    reviewer = User.objects.create_user(username="role-reviewer", password="password")
    approver = User.objects.create_user(username="role-approver", password="password")
    from django.contrib.auth.models import Group

    for user, group_name in (
        (classifier, "Classifier"),
        (reviewer, "Reviewer"),
    ):
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)

    client = pytest.importorskip("django.test").Client()
    client.login(username="role-reviewer", password="password")
    response = client.post(reverse("start-review", args=[item.pk]))
    assert response.status_code == 302

    client.login(username="role-classifier", password="password")
    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed", classifier)
    complete_classification(item, classifier)
    response = client.post(reverse("submit-for-review", args=[item.pk]))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == OpenItem.Status.COMPLETED

    client.login(username="role-approver", password="password")
    response = client.post(reverse("start-review", args=[item.pk]))
    assert response.status_code == 403


@pytest.mark.django_db
def test_corrected_wht_mapping_csv_imports_without_duplicate_keys(capsys) -> None:
    call_command("import_reference_data")
    call_command("import_wht_mapping")
    call_command("import_wht_mapping")

    assert WHTMapping.objects.count() == 47
    assert WHTMapping.objects.filter(
        supplier="VEN100187",
        country_code="GB",
        cost_category__code="RENTAL",
    ).count() == 1
    assert WHTMapping.objects.filter(
        supplier="VEN101707",
        country_code="LU",
        cost_category__code="INTEREST",
        wht_rate=Decimal("0.15"),
    ).count() == 1
    assert WHTMapping.objects.filter(
        supplier="VEN102110",
        country_code="GB",
        cost_category__code="ROYALTY",
        wht_rate=Decimal("0.1"),
    ).count() == 1
    output = capsys.readouterr()
    assert "created=47" in output.out
    assert "rejected=0" in output.out
    assert "Rejected duplicate Supplier + Country + Cost Category" not in output.err


@pytest.mark.django_db
def test_wht_rule_approval_requires_administrator_and_rejects_overlaps() -> None:
    cost_type = CostType.objects.create(code="APPROVAL", name="Approval")
    administrator, _ = User.objects.get_or_create(
        username="rule-approver",
        defaults={"is_staff": True},
    )
    administrator.is_staff = True
    administrator.save(update_fields=["is_staff"])
    administrator_group, _ = Group.objects.get_or_create(name="Administrator")
    administrator.groups.add(administrator_group)
    reviewer = User.objects.create_user(username="rule-reviewer")

    first = WHTMapping.objects.create(
        supplier="SUPPLIER",
        supplier_name="Supplier",
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="First",
        version=1,
        effective_from=date(2025, 1, 1),
    )
    second = WHTMapping.objects.create(
        supplier="SUPPLIER",
        supplier_name="Supplier",
        cost_category=cost_type,
        wht_rate=Decimal("0.12"),
        country_code="GB",
        source_title="Second",
        version=2,
        effective_from=date(2025, 6, 1),
    )

    with pytest.raises(PermissionDenied):
        approve_wht_rule(first.pk, reviewer)

    approved = approve_wht_rule(first.pk, administrator)
    assert approved.status == WHTMapping.Status.APPROVED
    assert approved.approved_by == administrator
    assert approved.approved_at is not None

    with pytest.raises(ValidationError, match="overlaps an approved rule"):
        approve_wht_rule(second.pk, administrator)


@pytest.mark.django_db
def test_wht_matching_uses_latest_approved_rule_version() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.get(accounting_document_number="708000230", line_number="1")
    cost_type = CostType.objects.create(code="TEST", name="TEST")
    SupplierCountry.objects.create(
        supplier_id=item.vendor_number,
        country_code="GB",
        supplier_name=item.vendor_name,
    )
    create_approved_wht_mapping(
        supplier=item.vendor_number,
        supplier_name=item.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="rule-1",
        source_row_number=1001,
    )
    create_approved_wht_mapping(
        supplier=item.vendor_number,
        supplier_name=item.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="rule-2",
        source_row_number=1002,
        version=2,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Test",
    )

    result = match_wht(item, decision)

    assert result.status == "MATCHED"
    assert result.candidate_count == 1
    assert result.mapping is not None
    assert result.mapping.version == 2


@pytest.mark.django_db
def test_mapping_selection_requires_exact_supplier_country_and_cost_type() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="EXACT_KEY", name="Exact key")
    other_cost_type = CostType.objects.create(code="OTHER_KEY", name="Other key")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    wrong_key_mappings = (
        {
            "supplier": f"{supplier_line.vendor_number}-OTHER",
            "country_code": "GB",
            "cost_category": cost_type,
        },
        {
            "supplier": supplier_line.vendor_number,
            "country_code": "US",
            "cost_category": cost_type,
        },
        {
            "supplier": supplier_line.vendor_number,
            "country_code": "GB",
            "cost_category": other_cost_type,
        },
    )
    for index, values in enumerate(wrong_key_mappings, start=1):
        create_approved_wht_mapping(
            supplier_name=supplier_line.vendor_name,
            wht_rate=Decimal("0.10"),
            source_title=f"Wrong-key mapping {index}",
            source_row_number=2400 + index,
            **values,
        )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Exact key test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert determination.matched_mapping is None
    assert determination.wht_amount_snapshot is None


@pytest.mark.django_db
def test_latest_approved_mapping_applies_even_before_its_effective_date() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="LATEST_DATE", name="Latest date")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Older rule",
        version=1,
        effective_from=date(1900, 1, 1),
        effective_to=item.posting_date,
    )
    latest_mapping = create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.20"),
        country_code="GB",
        source_title="Latest future-effective rule",
        version=2,
        effective_from=item.posting_date + timedelta(days=1),
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Latest approved version test",
    )

    determination = determine_wht(item, decision)

    assert determination.matched_mapping_id == latest_mapping.pk
    assert determination.rule_version_snapshot == "2"
    assert determination.wht_rate_snapshot == Decimal("0.20")
    assert determination.wht_amount_snapshot == (
        item.document_currency_amount * Decimal("0.20")
    ).quantize(Decimal("0.01"))


@pytest.mark.django_db
def test_mapping_approval_and_country_change_do_not_recalculate_existing_determination() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="IMMUTABLE_DET", name="Immutable")
    country = SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    first_mapping = create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Existing approved rule",
        version=1,
        effective_from=date(1900, 1, 1),
        effective_to=item.posting_date,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Determination preservation test",
    )
    original = determine_wht(item, decision)
    administrator = import_center_admin()
    new_mapping = WHTMapping.objects.create(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.20"),
        country_code="GB",
        source_title="Next approved rule",
        version=2,
        effective_from=item.posting_date + timedelta(days=1),
    )
    approve_wht_rule(new_mapping.pk, administrator)
    country.country_code = "US"
    country.save(update_fields=["country_code"])

    original.refresh_from_db()
    assert original.status == WHTDetermination.Status.MATCHED
    assert original.matched_mapping_id == first_mapping.pk
    assert original.rule_version_snapshot == "1"
    assert original.wht_rate_snapshot == Decimal("0.10")
    assert WHTDetermination.objects.filter(open_item=item).count() == 1


@pytest.mark.django_db
def test_wht_rounding_uses_round_half_up_for_positive_and_negative_bases() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="ROUNDING", name="Rounding")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Rounding rule",
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="ROUND_HALF_UP test",
    )

    results = []
    for base in (Decimal("1.05"), Decimal("-1.05")):
        item.document_currency_amount = base
        item.save(update_fields=["document_currency_amount"])
        results.append(determine_wht(item, decision))

    assert [row.wht_amount_snapshot for row in results] == [
        Decimal("0.11"),
        Decimal("-0.11"),
    ]


@pytest.mark.parametrize(
    ("difference", "expected_status"),
    [
        (Decimal("0.00"), "RECONCILED"),
        (Decimal("0.01"), "RECONCILED"),
        (Decimal("-0.01"), "RECONCILED"),
        (Decimal("0.02"), "DISCREPANCY"),
        (Decimal("-0.02"), "DISCREPANCY"),
    ],
)
def test_reconciliation_tolerance_boundary(difference, expected_status) -> None:
    from .services.reconciliation import reconcile_amounts

    calculated = Decimal("10.00")
    posted = Decimal("-10.00") + difference

    actual_difference, actual_status = reconcile_amounts(calculated, posted)

    assert actual_difference == difference
    assert actual_status == expected_status


@pytest.mark.django_db
def test_wht_determination_calculates_decimal_amount_and_snapshots_values() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.get(accounting_document_number="708000230", line_number="2")
    item.document_currency_amount = Decimal("513.55")
    item.save(update_fields=["document_currency_amount"])
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="TEST", name="TEST")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    mapping = create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="rule",
        source_row_number=1003,
    )
    decision = save_final_classification(
        item,
        cost_type,
        "Negative source posting",
    )
    complete_classification(item)

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.MATCHED
    assert determination.wht_amount_snapshot == Decimal("51.36")
    assert determination.wht_base_snapshot == item.document_currency_amount
    assert determination.currency_snapshot == "USD"
    assert determination.matched_mapping_id == mapping.id


@pytest.mark.django_db
def test_expense_line_wht_uses_document_supplier_and_browser_action() -> None:
    call_command("import_open_items")
    expense = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210020",
    )
    invoice = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210000",
    )
    SupplierCountry.objects.create(
        supplier_id=invoice.vendor_number,
        country_code="GB",
        supplier_name=invoice.vendor_name,
    )
    cost_type = CostType.objects.create(code="LINE_TEST", name="Line test")
    mapping = create_approved_wht_mapping(
        supplier=invoice.vendor_number,
        supplier_name=invoice.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Line test mapping",
        source_row_number=2301,
    )
    decision = ClassificationDecision.objects.create(
        open_item=expense,
        final_cost_type=cost_type,
        classification_reason="Line calculation test",
    )
    user = User.objects.create_user(username="line-calculator")
    group, _ = Group.objects.get_or_create(name="Classifier")
    user.groups.add(group)

    response = pytest.importorskip("django.test").Client()
    response.force_login(user)
    result = response.post(reverse("determine-wht", args=[expense.pk]))
    determination = expense.wht_determinations.get()

    assert result.status_code == 302
    assert determination.status == WHTDetermination.Status.MATCHED
    assert determination.matched_mapping_id == mapping.id
    assert determination.supplier_snapshot == invoice.vendor_number
    assert determination.supplier_country_snapshot == "GB"
    assert determination.wht_amount_snapshot == (
        expense.document_currency_amount * Decimal("0.10")
    ).quantize(Decimal("0.01"))


@pytest.mark.django_db
def test_review_workflow_records_actions_and_audit_events() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    save_final_classification(
        item,
        CostType.objects.get(code="RENTAL"),
        "Reviewed",
    )
    complete_classification(item)
    submit_for_review(item, comments="Ready for reviewer")
    start_review(item)

    assert item.status == OpenItem.Status.UNDER_REVIEW
    assert ReviewAction.objects.filter(open_item=item).count() == 2
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.REVIEW_STARTED,
    ).exists()


@pytest.mark.django_db
def test_approval_requires_resolved_wht_determination() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed")
    complete_classification(item)
    submit_for_review(item)
    start_review(item)

    with pytest.raises(ValueError, match="WHT determination"):
        complete_review(item)
    assert item.status == OpenItem.Status.UNDER_REVIEW


@pytest.mark.django_db
def test_unresolved_wht_cannot_be_approved_with_override_reason() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    user = User.objects.create_user(username="reviewer", password="test-password")
    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed", user)
    complete_classification(item, user)
    submit_for_review(item, user)
    start_review(item, user)
    WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="RENTAL",
        wht_base_snapshot=item.document_currency_amount,
        currency_snapshot=item.document_currency,
    )

    with pytest.raises(ValueError, match="unresolved WHT exception"):
        complete_review(
            item,
            user,
            comments="Review attempt",
            override_reason="Supplier-country mapping requires documented exception.",
        )
    item.refresh_from_db()

    assert item.status == OpenItem.Status.UNDER_REVIEW
    assert not ReviewAction.objects.filter(
        open_item=item,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
    ).exists()


@pytest.mark.django_db
def test_browser_review_actions_require_login_and_update_status() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    from django.contrib.auth.models import Group

    user = User.objects.create_user(username="reviewer", password="test-password")
    group, _ = Group.objects.get_or_create(name="Classifier")
    user.groups.add(group)
    client = pytest.importorskip("django.test").Client()

    response = client.post(reverse("submit-for-review", args=[item.pk]))
    assert response.status_code == 302
    assert response.url.startswith("/login/?next=")

    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed", user)
    complete_classification(item, user)
    client.login(username="reviewer", password="test-password")
    response = client.post(reverse("submit-for-review", args=[item.pk]))
    item.refresh_from_db()

    assert response.status_code == 302
    assert response.url == reverse(
        "open-item-document",
        args=[item.entity_code, item.accounting_document_number],
    )
    assert item.status == OpenItem.Status.COMPLETED


@pytest.mark.parametrize(
    "route_name",
    ["submit-for-review", "start-review", "complete-review", "return-item"],
)
@pytest.mark.django_db
def test_retired_line_review_routes_cannot_bypass_invoice_finalization(
    route_name: str,
) -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    client = transaction_user_client(username="retired-review-route-user")
    user = User.objects.get(username="retired-review-route-user")
    save_final_classification(
        item,
        CostType.objects.get(code="RENTAL"),
        "Retired review route protection",
        user,
    )
    complete_classification(item, user)

    response = client.post(reverse(route_name, args=[item.pk]))
    item.refresh_from_db()

    assert response.status_code == 302
    assert item.status == OpenItem.Status.COMPLETED
    assert not ReviewAction.objects.filter(
        open_item=item,
        action_type__in=[
            ReviewAction.ActionType.REVIEW_REQUESTED,
            ReviewAction.ActionType.REVIEW_STARTED,
            ReviewAction.ActionType.REVIEW_COMPLETED,
            ReviewAction.ActionType.RETURNED,
        ],
    ).exists()


@pytest.mark.django_db
def test_return_requires_comment_and_reclassification_can_resume() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed")
    complete_classification(item)
    submit_for_review(item)
    start_review(item)

    with pytest.raises(ValueError, match="return comment"):
        return_item(item)
    return_item(item, comments="Please correct the classification.")

    assert item.status == OpenItem.Status.RETURNED
    save_final_classification(item, CostType.objects.get(code="LABOR"), "Corrected")
    assert item.status == OpenItem.Status.IN_CLASSIFICATION


@pytest.mark.django_db
def test_approved_item_cannot_be_reclassified() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    save_final_classification(item, CostType.objects.get(code="RENTAL"), "Reviewed")
    item.status = OpenItem.Status.REVIEWED
    item.save(update_fields=["status"])

    with pytest.raises(ValueError, match="cannot be edited"):
        save_final_classification(item, CostType.objects.get(code="LABOR"), "Changed")


@pytest.mark.django_db
def test_missing_mapping_requires_review() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.get(accounting_document_number="708000230", line_number="2")
    cost_type = CostType.objects.create(code="TEST", name="TEST")
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert determination.wht_amount_snapshot is None
    item.status = OpenItem.Status.COMPLETED
    item.save(update_fields=["status"])
    user = User.objects.create_user(username="missing-mapping-finalizer")
    group, _ = Group.objects.get_or_create(name="Transaction User")
    user.groups.add(group)
    with pytest.raises(ValueError, match="WHT determination is unresolved"):
        finalize_invoice(item, user)


@pytest.mark.django_db
def test_completed_snapshot_reconciles_reference_document_600008420() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-105.49"),),
    )

    assert snapshot.document_amount == Decimal("-949.44")
    assert snapshot.document_currency == "GBP"
    assert snapshot.calculated_wht == Decimal("105.49")
    assert snapshot.booked_wht == Decimal("-105.49")
    assert snapshot.modification_required == Decimal("0.00")
    assert serialize_review_snapshot(snapshot)["reconciliation_status"] == "Reconciled"


@pytest.mark.django_db
def test_completed_snapshot_reconciles_reference_document_600008559() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008559",
        invoice_amount=Decimal("-1252.00"),
        expense_amount=Decimal("1252.00"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-125.20"),),
    )

    assert snapshot.document_amount == Decimal("-1252.00")
    assert snapshot.calculated_wht == Decimal("125.20")
    assert snapshot.booked_wht == Decimal("-125.20")
    assert snapshot.modification_required == Decimal("0.00")


@pytest.mark.parametrize(
    ("difference", "expected_status"),
    [
        (Decimal("0.00"), "Reconciled"),
        (Decimal("0.01"), "Reconciled"),
        (Decimal("-0.01"), "Reconciled"),
        (Decimal("0.02"), "Discrepancy"),
        (Decimal("-0.02"), "Discrepancy"),
    ],
)
@pytest.mark.django_db
def test_snapshot_reconciliation_uses_one_cent_tolerance(
    difference,
    expected_status,
) -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-10.00") + difference,),
    )

    values = serialize_review_snapshot(snapshot)

    assert snapshot.modification_required == difference
    assert values["reconciliation_status"] == expected_status


@pytest.mark.django_db
def test_completed_snapshot_handles_unbooked_reference_document_600008437() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008437",
        invoice_amount=Decimal("-6270.00"),
        expense_amount=Decimal("6270.00"),
        rate=Decimal("0.10"),
    )

    assert snapshot.document_amount == Decimal("-6270.00")
    assert snapshot.calculated_wht == Decimal("627.00")
    assert snapshot.booked_wht == Decimal("0.00")
    assert snapshot.has_wht_posting is False
    assert snapshot.modification_required == Decimal("627.00")
    assert serialize_review_snapshot(snapshot)["booked_wht"] == Decimal("0.00")


@pytest.mark.django_db
def test_completed_snapshot_handles_unbooked_reference_document_600008459() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008459",
        invoice_amount=Decimal("-627.00"),
        expense_amount=Decimal("627.00"),
        rate=Decimal("0.10"),
    )

    assert snapshot.document_amount == Decimal("-627.00")
    assert snapshot.calculated_wht == Decimal("62.70")
    assert snapshot.booked_wht == Decimal("0.00")
    assert snapshot.modification_required == Decimal("62.70")


@pytest.mark.django_db
def test_completed_snapshot_missing_mapping_is_null_not_zero() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-1054.90"),
        expense_amount=Decimal("1054.90"),
        rate=None,
    )

    assert snapshot.calculated_wht is None
    assert snapshot.modification_required is None
    assert snapshot.mapping_required is True
    assert "No unique approved WHT mapping" in snapshot.exception_reason
    assert serialize_review_snapshot(snapshot)["reconciliation_status"] == (
        "Pending / Provisional"
    )


@pytest.mark.django_db
def test_explicit_zero_percent_mapping_is_valid_zero_result() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=Decimal("0.00"),
    )

    assert snapshot.calculated_wht == Decimal("0.00")
    assert snapshot.modification_required == Decimal("0.00")
    assert snapshot.mapping_required is False


@pytest.mark.django_db
def test_missing_mapping_for_tr_supplier_is_still_an_exception() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=None,
        country="TR",
    )

    assert snapshot.calculated_wht is None
    assert snapshot.modification_required is None
    assert snapshot.mapping_required is True
    assert "No unique approved WHT mapping" in snapshot.exception_reason


@pytest.mark.django_db
def test_multiple_booked_wht_lines_are_summed_with_sap_sign() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-1054.90"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
        booked_amounts=(
            Decimal("-40.00"),
            Decimal("-65.50"),
            Decimal("0.01"),
        ),
    )

    assert snapshot.calculated_wht == Decimal("105.49")
    assert snapshot.booked_wht == Decimal("-105.49")
    assert snapshot.modification_required == Decimal("0.00")


@pytest.mark.django_db
def test_invoice_control_lines_keep_signed_amount_and_currency_conflicts_raise_exception() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    invoice = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="600008420",
        gl_account="210000",
    )
    second_invoice = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="600008420",
        gl_account="210000",
    )
    second_invoice.pk = None
    second_invoice.id = None
    second_invoice.accounting_document_number = "600008420"
    second_invoice.line_number = "4"
    second_invoice.document_currency = "EUR"
    second_invoice.document_currency_amount = Decimal("-20.00")
    second_invoice.save()
    expense = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="600008420",
        gl_account="210020",
    )
    completion_action = ReviewAction.objects.create(
        open_item=expense,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
    )
    snapshot = create_review_document_snapshot(
        expense,
        completion_action=completion_action,
    )

    assert invoice.document_currency_amount == Decimal("949.44")
    assert snapshot.document_amount is None
    assert snapshot.document_currency == ""
    assert "multiple document currencies" in snapshot.exception_reason


@pytest.mark.django_db
def test_reset_preserves_reference_data_and_users_and_removes_imported_transactions(
    tmp_path,
) -> None:
    cost_type, _ = CostType.objects.get_or_create(
        code="RESET_TEST",
        defaults={"name": "Reset test"},
    )
    SupplierCountry.objects.create(
        supplier_id="PRESERVE-SUPPLIER",
        country_code="GB",
        supplier_name="Preserved supplier",
    )
    create_approved_wht_mapping(
        supplier="PRESERVE-SUPPLIER",
        supplier_name="Preserved supplier",
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Preserved WHT rule",
    )
    call_command("import_open_items")
    call_command("import_material_data")
    call_command("import_cost_type_decisions")
    user = User.objects.create_user(username="reset-preserved-user")
    group, _ = Group.objects.get_or_create(name="Reviewer")
    user.groups.add(group)
    before = {
        "users": User.objects.count(),
        "groups": Group.objects.count(),
        "cost_types": CostType.objects.count(),
        "countries": SupplierCountry.objects.count(),
        "mappings": WHTMapping.objects.count(),
    }
    staged_name = f"{uuid.uuid4().hex}.csv"
    (tmp_path / staged_name).write_text("synthetic staged CSV", encoding="utf-8")
    ImportUploadSession.objects.create(
        token=uuid.uuid4(),
        uploaded_by=user,
        material_staged_name=staged_name,
        material_original_name="staged.csv",
        material_sha256="0" * 64,
    )

    with override_settings(
        WHT_RESET_BACKUP_DIR=tmp_path,
        WHT_IMPORT_STAGING_DIR=tmp_path,
    ):
        call_command("reset_wht_test_data", confirm=True)

    backups = list(tmp_path.glob("wht-before-reset-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM transactions_openitem").fetchone()[0] > 0
    assert OpenItem.objects.count() == 0
    assert MaterialData.objects.count() == 0
    assert ImportBatch.objects.count() == 0
    assert ImportUploadSession.objects.count() == 0
    assert not (tmp_path / staged_name).exists()
    assert User.objects.count() == before["users"]
    assert Group.objects.count() == before["groups"]
    assert CostType.objects.count() == before["cost_types"]
    assert SupplierCountry.objects.count() == before["countries"]
    assert WHTMapping.objects.count() == before["mappings"]


@pytest.mark.django_db
def test_completed_items_ui_and_excel_export_use_identical_snapshot_values() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-105.49"),),
    )
    OpenItem.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).update(
        document_currency_amount=Decimal("-9999.00"),
        document_currency="EUR",
        vendor_number="LIVE-SOURCE-SUPPLIER",
        vendor_name="Live source supplier",
    )
    reviewer = User.objects.create_user(username="completed-items-reviewer")
    reviewer_group, _ = Group.objects.get_or_create(name="Reviewer")
    reviewer.groups.add(reviewer_group)
    client = pytest.importorskip("django.test").Client()
    client.force_login(reviewer)

    ui_response = client.get(reverse("review-completed-items"))
    export_response = client.get(reverse("review-completed-export"))

    assert ui_response.status_code == 200
    assert export_response.status_code == 200
    ui_text = ui_response.content.decode()
    assert "-949.44" in ui_text
    assert "105.49" in ui_text
    assert "-105.49" in ui_text
    assert "0.00" in ui_text
    workbook = load_workbook(BytesIO(export_response.content), data_only=False)
    exported = next(
        row
        for row in workbook["Completed Invoices"].iter_rows(min_row=2, values_only=True)
        if row[1] == "600008420"
    )
    assert Decimal(str(exported[8])) == snapshot.document_amount
    assert exported[2] == snapshot.supplier
    assert exported[3] == snapshot.vendor_name
    assert exported[4] == snapshot.supplier_country
    assert exported[7] == snapshot.document_currency
    assert Decimal(str(exported[12])) == snapshot.calculated_wht
    assert Decimal(str(exported[13])) == snapshot.booked_wht
    assert Decimal(str(exported[14])) == snapshot.modification_required
    assert exported[15] == "Reconciled"
    assert "Reconciled" in ui_text
    assert all(
        isinstance(exported[index], (int, float, Decimal))
        for index in (12, 13, 14)
    )


@pytest.mark.django_db
def test_completed_import_does_not_replace_or_recalculate_snapshot(
    tmp_path,
) -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    completed_line = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    row = open_item_csv_row(completed_line)
    csv_file = write_csv_file(
        tmp_path,
        "completed-line-reimport.csv",
        tuple(row.keys()),
        [row],
    )
    before_snapshot_count = ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count()

    call_command("import_open_items", str(csv_file))

    completed_line.refresh_from_db()
    snapshot.refresh_from_db()
    batch = ImportBatch.objects.get(source_filename="completed-line-reimport.csv")
    assert completed_line.status == OpenItem.Status.REVIEWED
    assert batch.rows_created == 0
    assert batch.rows_skipped == 1
    assert batch.rows_rejected == 0
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count() == before_snapshot_count
    assert snapshot.is_current
    assert snapshot.document_amount == Decimal("-949.44")
    assert snapshot.calculated_wht == Decimal("105.49")


@pytest.mark.django_db
def test_new_valid_completion_adds_snapshot_version_without_rewriting_history() -> None:
    first_snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    completed_line = OpenItem.objects.get(
        entity_code=first_snapshot.entity_code,
        accounting_document_number=first_snapshot.accounting_document_number,
        gl_account="210020",
    )
    completed_line.status = OpenItem.Status.UNDER_REVIEW
    completed_line.save(update_fields=["status"])

    complete_review(completed_line)

    first_snapshot.refresh_from_db()
    second_snapshot = ReviewDocumentSnapshot.objects.get(
        entity_code=first_snapshot.entity_code,
        accounting_document_number=first_snapshot.accounting_document_number,
        is_current=True,
    )
    assert first_snapshot.pk != second_snapshot.pk
    assert first_snapshot.is_current is False
    assert first_snapshot.calculation_version == 1
    assert first_snapshot.document_amount == Decimal("-949.44")
    assert second_snapshot.calculation_version == 2
    assert second_snapshot.document_amount == first_snapshot.document_amount


@pytest.mark.django_db
def test_reference_updates_and_mapping_approval_preserve_completed_history(
    tmp_path,
) -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    before_snapshot_count = ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count()
    before_line_snapshot_count = ReviewLineCalculationSnapshot.objects.filter(
        document_snapshot=snapshot
    ).count()
    before_determination_count = WHTDetermination.objects.filter(
        open_item__entity_code=snapshot.entity_code,
        open_item__accounting_document_number=snapshot.accounting_document_number,
    ).count()
    administrator = import_center_admin()
    request = RequestFactory().post("/admin/reference_data/suppliercountry/")
    request.user = administrator
    supplier_country = SupplierCountry.objects.get(supplier_id=snapshot.supplier)
    supplier_country.country_code = "US"
    admin.site._registry[SupplierCountry].save_model(
        request,
        supplier_country,
        form=None,
        change=True,
    )
    snapshot.refresh_from_db()
    assert snapshot.supplier_country == "GB"
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count() == before_snapshot_count

    country_file = write_csv_file(
        tmp_path,
        "updated-countries.csv",
        ("Supplier", "Country", "Supplier Name"),
        [
            {
                "Supplier": snapshot.supplier,
                "Country": "CA",
                "Supplier Name": snapshot.vendor_name,
            }
        ],
    )
    cost_type_file = write_csv_file(
        tmp_path,
        "cost-types.csv",
        ("Cost Type",),
        [{"Cost Type": "RENTAL"}],
    )
    call_command(
        "import_reference_data",
        countries=str(country_file),
        cost_decisions=str(cost_type_file),
    )

    old_rule = WHTMapping.objects.get(
        supplier=snapshot.supplier,
        country_code="GB",
        cost_category__code="RENTAL",
        status=WHTMapping.Status.APPROVED,
    )
    new_rule = WHTMapping.objects.create(
        supplier=snapshot.supplier,
        supplier_name=snapshot.vendor_name,
        cost_category=old_rule.cost_category,
        wht_rate=Decimal("0.20"),
        country_code="US",
        source_title="Synthetic next-version rule",
        version=1,
        effective_from=timezone.localdate(),
    )
    approve_wht_rule(new_rule.pk, administrator)

    snapshot.refresh_from_db()
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count() == before_snapshot_count
    assert ReviewLineCalculationSnapshot.objects.filter(
        document_snapshot=snapshot
    ).count() == before_line_snapshot_count
    assert WHTDetermination.objects.filter(
        open_item__entity_code=snapshot.entity_code,
        open_item__accounting_document_number=snapshot.accounting_document_number,
    ).count() == before_determination_count
    assert snapshot.is_current
    assert snapshot.supplier_country == "GB"
    assert snapshot.calculated_wht == Decimal("105.49")
    line_snapshot = ReviewLineCalculationSnapshot.objects.get(
        document_snapshot=snapshot
    )
    assert line_snapshot.mapping_version == str(old_rule.version)


@pytest.mark.django_db
def test_dashboard_pending_while_any_required_line_lacks_determination() -> None:
    call_command("import_open_items")
    item = classifiable_item()

    summary = next(
        row
        for row in summarize_documents(
            dashboard_queryset().filter(
                entity_code=item.entity_code,
                accounting_document_number=item.accounting_document_number,
            )
        )
        if row.document_number == item.accounting_document_number
    )

    assert summary.calculated_wht is None
    assert summary.reconciliation_status == "PENDING"
    client = transaction_user_client(username="pending-reconciliation-reader")
    detail = client.get(
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        )
    )
    assert b"Pending / Provisional" in detail.content


@pytest.mark.django_db
def test_dashboard_detail_and_exports_share_saved_determination_reconciliation() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-105.50"), Decimal("0.03")),
    )
    determination = ReviewLineCalculationSnapshot.objects.get(
        document_snapshot=snapshot
    )
    saved_mapping_version = determination.mapping_version
    SupplierCountry.objects.filter(supplier_id=snapshot.supplier).update(
        country_code="US"
    )
    create_approved_wht_mapping(
        supplier=snapshot.supplier,
        supplier_name=snapshot.vendor_name,
        cost_category=CostType.objects.get(code="RENTAL"),
        wht_rate=Decimal("0.20"),
        country_code="US",
        source_title="Later reference mapping",
        version=1,
        effective_from=timezone.localdate(),
    )
    client = transaction_user_client(username="reconciliation-reader")

    summary = next(
        row
        for row in summarize_documents(
            dashboard_queryset().filter(
                entity_code=snapshot.entity_code,
                accounting_document_number=snapshot.accounting_document_number,
            )
        )
        if row.document_number == snapshot.accounting_document_number
    )
    list_response = client.get(
        reverse("review-completed-items"),
        {
            "document": snapshot.accounting_document_number,
            "reconciliation_status": "DISCREPANCY",
        },
    )
    detail_response = client.get(
        reverse(
            "open-item-document",
            args=[snapshot.entity_code, snapshot.accounting_document_number],
        )
    )
    open_export_response = client.get(
        reverse("review-completed-export"),
        {"document": snapshot.accounting_document_number},
    )
    completed_response = client.get(reverse("review-completed-items"))
    completed_export_response = client.get(reverse("review-completed-export"))
    determination.refresh_from_db()
    snapshot.refresh_from_db()

    assert summary.calculated_wht == Decimal("105.49")
    assert summary.posted_wht == Decimal("-105.47")
    assert summary.difference == Decimal("0.02")
    assert summary.reconciliation_status == "DISCREPANCY"
    assert list_response.status_code == 200
    assert list_response.context["page_obj"].paginator.count == 1
    assert b"Discrepancy" in detail_response.content
    open_workbook = load_workbook(
        BytesIO(open_export_response.content),
        data_only=False,
    )
    open_row = next(open_workbook["Completed Invoices"].iter_rows(min_row=2))
    assert Decimal(str(open_row[12].value)) == Decimal("105.49")
    assert Decimal(str(open_row[13].value)) == Decimal("-105.47")
    assert Decimal(str(open_row[14].value)) == Decimal("0.02")
    assert open_row[15].value == "Discrepancy"
    assert completed_response.status_code == 200
    assert b"Discrepancy" in completed_response.content
    completed_workbook = load_workbook(
        BytesIO(completed_export_response.content),
        data_only=False,
    )
    completed_row = next(
        row
        for row in completed_workbook["Completed Invoices"].iter_rows(
            min_row=2,
            values_only=True,
        )
        if row[1] == "600008420"
    )
    assert completed_row[15] == "Discrepancy"
    assert determination.mapping_version == saved_mapping_version == "1"
    assert snapshot.calculated_wht == Decimal("105.49")
    assert snapshot.booked_wht == Decimal("-105.47")
    assert snapshot.modification_required == Decimal("0.02")


@pytest.mark.django_db
def test_negative_amount_is_calculated_with_its_source_sign() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    item.document_currency_amount = Decimal("-100.05")
    item.save(update_fields=["document_currency_amount"])
    assert item.is_classifiable is True
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="TEST", name="TEST")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="rule",
        source_row_number=1004,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.MATCHED
    assert determination.wht_base_snapshot == Decimal("-100.05")
    assert determination.wht_amount_snapshot == Decimal("-10.01")


@pytest.mark.django_db
def test_unconfigured_gl_accounts_are_eligible_for_nonzero_positive_amounts() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    item.gl_account = "UNCONFIGURED-GL"
    item.document_currency_amount = Decimal("12.34")
    item.save(update_fields=["gl_account", "document_currency_amount"])

    assert not GLEligibility.objects.filter(gl_account=item.gl_account).exists()
    assert item.is_classifiable is True


@pytest.mark.django_db
def test_unconfigured_gl_accounts_are_eligible_for_nonzero_negative_amounts() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    item.gl_account = "UNCONFIGURED-NEGATIVE-GL"
    item.document_currency_amount = Decimal("-12.34")
    item.save(update_fields=["gl_account", "document_currency_amount"])

    assert item.is_classifiable is True


@pytest.mark.django_db
def test_explicit_gl_eligibility_configuration_controls_nonzero_lines() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    item.gl_account = "CONFIGURED-ELIGIBLE-GL"
    item.save(update_fields=["gl_account"])
    GLEligibility.objects.create(gl_account=item.gl_account, is_eligible=True)
    assert item.is_classifiable is True

    item.gl_account = "CONFIGURED-EXCLUDED-GL"
    item.save(update_fields=["gl_account"])
    GLEligibility.objects.create(gl_account=item.gl_account, is_eligible=False)
    assert item.is_classifiable is False


@pytest.mark.django_db
def test_zero_amount_lines_are_excluded_from_classification_and_wht() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    GLEligibility.objects.create(
        gl_account=item.gl_account,
        is_eligible=True,
    )
    item.document_currency_amount = Decimal("0.00")
    item.save(update_fields=["document_currency_amount"])
    cost_type = CostType.objects.create(code="ZERO_EXCLUDED", name="Zero excluded")

    assert item.is_classifiable is False
    with pytest.raises(ValueError, match="non-zero postings"):
        save_final_classification(item, cost_type, "Zero line")
    with pytest.raises(ValueError, match="non-zero postings"):
        determine_wht(
            item,
            ClassificationDecision(
                open_item=item,
                final_cost_type=cost_type,
                classification_reason="Zero line",
            ),
        )


@pytest.mark.django_db
def test_excluded_gl_is_blocked_by_classification_service_and_detail_post() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    cost_type = CostType.objects.get(code="RENTAL")
    GLEligibility.objects.create(gl_account=item.gl_account, is_eligible=False)
    client = transaction_user_client()

    assert item.is_classifiable is False
    with pytest.raises(ValueError, match="eligible G/L accounts"):
        save_final_classification(item, cost_type, "Should be blocked")
    decision = ClassificationDecision(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Should be blocked",
    )
    with pytest.raises(ValueError, match="eligible G/L accounts"):
        determine_wht(item, decision)
    item.status = OpenItem.Status.COMPLETED
    with pytest.raises(ValueError, match="eligible G/L accounts"):
        submit_for_review(item)

    response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": cost_type.pk,
            "classification_reason": "Should be blocked",
        },
    )
    assert response.status_code == 302
    assert not item.classification_decisions.exists()


@pytest.mark.django_db
def test_gl_configuration_changes_do_not_recalculate_determinations_or_snapshots() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008421",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    expense = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    determination = expense.wht_determinations.get()
    original_determination_values = (
        determination.status,
        determination.wht_rate_snapshot,
        determination.wht_base_snapshot,
        determination.wht_amount_snapshot,
    )
    original_snapshot_values = (
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
    )
    original_snapshot_count = ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count()

    GLEligibility.objects.create(gl_account=expense.gl_account, is_eligible=False)

    expense.refresh_from_db()
    determination.refresh_from_db()
    snapshot.refresh_from_db()
    assert expense.is_classifiable is False
    assert (
        determination.status,
        determination.wht_rate_snapshot,
        determination.wht_base_snapshot,
        determination.wht_amount_snapshot,
    ) == original_determination_values
    assert (
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
    ) == original_snapshot_values
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
    ).count() == original_snapshot_count


@pytest.mark.django_db
def test_zero_rate_mapping_is_a_distinct_no_wht_determination() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.get(accounting_document_number="708000230", line_number="2")
    supplier_line = document_supplier_line(item)
    cost_type = CostType.objects.create(code="TEST", name="TEST")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0"),
        country_code="GB",
        source_title="rule",
        source_row_number=1005,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.NO_WHT
    assert determination.matched_mapping_id is not None
    assert determination.rule_version_snapshot == "1"
    assert determination.wht_amount_snapshot == Decimal("0.00")


@pytest.mark.django_db
def test_domestic_tr_supplier_has_no_wht_without_mapping() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    OpenItem.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    ).update(
        vendor_number="TR-SYNTHETIC",
        vendor_name="Synthetic Turkish Supplier",
    )
    item.refresh_from_db()
    SupplierCountry.objects.create(
        supplier_id=item.vendor_number,
        country_code="TR",
        supplier_name=item.vendor_name,
    )
    cost_type = CostType.objects.create(code="TR_TEST", name="TR Test")
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Domestic supplier test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert determination.wht_rate_snapshot is None
    assert determination.wht_amount_snapshot is None


@pytest.mark.django_db
def test_explicit_tr_mapping_overrides_domestic_no_wht_default() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    OpenItem.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    ).update(
        vendor_number="TR-SYNTHETIC",
        vendor_name="Synthetic Turkish Supplier",
    )
    item.refresh_from_db()
    SupplierCountry.objects.create(
        supplier_id=item.vendor_number,
        country_code="TR",
        supplier_name=item.vendor_name,
    )
    cost_type = CostType.objects.create(code="TR_MAPPED", name="TR Mapped")
    mapping = create_approved_wht_mapping(
        supplier=item.vendor_number,
        supplier_name=item.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="TR",
        source_title="Explicit domestic exception",
        source_row_number=2001,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Domestic override test",
    )

    determination = determine_wht(item, decision)

    assert determination.status == WHTDetermination.Status.MATCHED
    assert determination.matched_mapping_id == mapping.id
    assert determination.wht_amount_snapshot == (
        item.document_currency_amount * Decimal("0.10")
    ).quantize(Decimal("0.01"))


@pytest.mark.django_db
def test_manual_rate_requires_administrator_authorization_and_reason() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    cost_type = CostType.objects.create(code="MANUAL", name="Manual")
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Manual rate test",
    )
    ordinary_user = User.objects.create_user(username="manual-ordinary-user")

    with pytest.raises(PermissionError, match="Only an Administrator"):
        determine_wht(
            item,
            decision,
            manual_rate=Decimal("0.05"),
            manual_reason="Documented exception",
            user=ordinary_user,
        )

    with pytest.raises(ValueError, match="reason"):
        determine_wht(
            item,
            decision,
            manual_rate=Decimal("0.05"),
            user=ordinary_user,
        )

    reviewer = import_center_admin()
    supplier = document_supplier_line(item)
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier.vendor_name,
        },
    )
    determination = determine_wht(
        item,
        decision,
        manual_rate=Decimal("0.05"),
        manual_reason="Approved documented exception",
        user=reviewer,
    )

    assert determination.status == WHTDetermination.Status.ADMIN_RESOLVED
    assert determination.rate_source == "MANUAL"
    assert determination.manual_override_reason == "Approved documented exception"
    assert not has_valid_determination(determination)


@pytest.mark.django_db
def test_wht_calculation_versions_and_mapping_row_snapshot_are_immutable() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = OpenItem.objects.get(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
        vendor_number__gt="",
    )
    cost_type = CostType.objects.create(code="VERSIONED", name="Versioned")
    SupplierCountry.objects.create(
        supplier_id=supplier_line.vendor_number,
        country_code="GB",
        supplier_name=supplier_line.vendor_name,
    )
    mapping = create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Version one",
        source_row_number=2101,
        effective_to=item.posting_date,
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Version test",
    )

    first = determine_wht(item, decision)
    mapping.wht_rate = Decimal("0.20")
    with pytest.raises(ValidationError, match="immutable"):
        mapping.save(update_fields=["wht_rate"])
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.20"),
        country_code="GB",
        source_title="Version two",
        source_row_number=2102,
        version=2,
        effective_from=item.posting_date + timedelta(days=1),
    )
    item.posting_date += timedelta(days=1)
    item.save(update_fields=["posting_date"])
    second = determine_wht(item, decision)

    assert first.calculation_version == 1
    assert second.calculation_version == 2
    assert first.wht_rate_snapshot == Decimal("0.10")
    assert first.rule_version_snapshot == "1"
    assert second.wht_rate_snapshot == Decimal("0.20")
    assert second.rule_version_snapshot == "2"


@pytest.mark.django_db
def test_dashboard_marks_mixed_currency_documents_for_review() -> None:
    call_command("import_open_items")
    expense = OpenItem.objects.get(
        accounting_document_number="708000230",
        line_number="2",
    )
    expense.document_currency = "EUR"
    expense.save(update_fields=["document_currency"])

    summary = next(
        row
        for row in summarize_documents(
            dashboard_queryset().filter(
                accounting_document_number="708000230"
            )
        )
        if row.document_number == "708000230"
    )

    assert summary.currency == ""
    assert summary.calculated_wht is None
    assert summary.reconciliation_status == "PENDING"


@pytest.mark.django_db
def test_dashboard_query_count_stays_bounded(django_assert_num_queries) -> None:
    call_command("import_open_items")

    with django_assert_num_queries(8):
        summarize_documents(dashboard_queryset())


@pytest.mark.django_db
def test_filtered_export_contains_only_matching_documents() -> None:
    call_command("import_open_items")
    response = transaction_user_client().get(
        reverse("open-item-export"),
        {"vendor": "VEN100185"},
    )

    workbook = load_workbook(filename=BytesIO(response.content), data_only=False)
    sheet = workbook["Active Invoices"]
    assert sheet.max_row == (
        OpenItem.objects.filter(vendor_number="VEN100185")
        .values("entity_code", "accounting_document_number")
        .distinct()
        .count()
        + 1
    )
    assert all(row[2].value == "VEN100185" for row in sheet.iter_rows(min_row=2))


@pytest.mark.django_db
def test_export_resolves_supplier_country_and_blank_wht_for_missing_mapping() -> None:
    call_command("import_open_items")
    invoice = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210000",
    )
    service = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210020",
    )
    SupplierCountry.objects.create(
        supplier_id=invoice.vendor_number,
        country_code="GB",
        supplier_name=invoice.vendor_name,
    )
    cost_type = CostType.objects.create(code="EXPORT_MISSING", name="Export missing")
    ClassificationDecision.objects.create(
        open_item=service,
        final_cost_type=cost_type,
        classification_reason="Export test",
    )
    service.status = OpenItem.Status.COMPLETED
    service.save(update_fields=["status"])

    response = transaction_user_client().get(
        reverse("open-item-export"),
        {"document": "708000226"},
    )
    workbook = load_workbook(filename=BytesIO(response.content), data_only=False)
    row = next(workbook["Active Invoices"].iter_rows(min_row=2))

    assert workbook["Active Invoices"].max_column == len(HEADERS)
    assert row[2].value == invoice.vendor_number
    assert row[3].value == invoice.vendor_name
    assert Decimal(str(row[8].value)) == Decimal("933.26")
    assert row[4].value == "GB"
    assert row[12].value is None
    assert Decimal(str(row[13].value)) == Decimal("-62.45")
    assert row[14].value is None


@pytest.mark.django_db
def test_export_aggregates_multiple_service_lines_and_posted_wht() -> None:
    call_command("import_open_items")
    invoice = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210000",
    )
    service = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210020",
    )
    extra_service = OpenItem.objects.get(pk=service.pk)
    extra_service.pk = None
    extra_service.line_number = "5"
    extra_service.document_currency_amount = Decimal("100.00")
    extra_service.company_code_currency_amount = Decimal("100.00")
    extra_service.group_currency_amount = Decimal("100.00")
    extra_service.save()
    posted = OpenItem.objects.get(pk=service.pk)
    posted.pk = None
    posted.line_number = "6"
    posted.gl_account = "229220"
    posted.document_currency_amount = Decimal("-116.50")
    posted.company_code_currency_amount = Decimal("-116.50")
    posted.group_currency_amount = Decimal("-116.50")
    posted.save()
    SupplierCountry.objects.create(
        supplier_id=invoice.vendor_number,
        country_code="GB",
        supplier_name=invoice.vendor_name,
    )
    cost_type = CostType.objects.create(code="EXPORT_MAPPED", name="Export mapped")
    create_approved_wht_mapping(
        supplier=invoice.vendor_number,
        supplier_name=invoice.vendor_name,
        cost_category=cost_type,
        country_code="GB",
        wht_rate=Decimal("0.10"),
        source_title="Export mapping",
        source_row_number=2201,
    )
    for item in (service, extra_service):
        ClassificationDecision.objects.create(
            open_item=item,
            final_cost_type=cost_type,
            classification_reason="Export aggregation test",
        )
        item.status = OpenItem.Status.COMPLETED
        item.save(update_fields=["status"])
        determine_wht(item, item.classification_decisions.get(is_current=True))

    response = transaction_user_client().get(
        reverse("open-item-export"),
        {"document": "708000226"},
    )
    workbook = load_workbook(filename=BytesIO(response.content), data_only=False)
    row = next(workbook["Active Invoices"].iter_rows(min_row=2))

    assert Decimal(str(row[12].value)) == Decimal("116.50")
    assert Decimal(str(row[13].value)) == Decimal("-178.95")
    assert Decimal(str(row[14].value)) == Decimal("-62.45")


@pytest.mark.django_db
def test_export_includes_all_filtered_documents_across_dashboard_pages() -> None:
    call_command("import_open_items")
    template = OpenItem.objects.first()
    for index in range(2):
        item = OpenItem.objects.get(pk=template.pk)
        item.pk = None
        item.accounting_document_number = f"EXPORT{index}"
        item.line_number = "1"
        item.save()

    client = transaction_user_client()
    dashboard = client.get(reverse("open-item-list"))
    export = client.get(reverse("open-item-export"))
    workbook = load_workbook(filename=BytesIO(export.content), data_only=False)

    assert dashboard.context["page_obj"].paginator.num_pages == 2
    assert workbook["Active Invoices"].max_row == 22


def import_center_admin() -> User:
    user = User.objects.create_user(
        username="import-center-admin",
        password="test-password",
        is_staff=True,
    )
    group, _ = Group.objects.get_or_create(name="Administrator")
    user.groups.add(group)
    return user


def import_center_file(name: str, headers: tuple[str, ...], row: dict[str, str]):
    return import_center_rows(name, headers, [row])


def import_center_rows(
    name: str,
    headers: tuple[str, ...],
    rows: list[dict[str, str]],
):
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return SimpleUploadedFile(
        name,
        output.getvalue().encode("utf-8"),
        content_type="text/csv",
    )


def write_csv_file(tmp_path, name, headers, rows):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


def import_center_files():
    material_headers = (
        "MaterialData Key",
        "Purchasing Document",
        "Item",
        "Plant",
        "Order Unit",
        "Document Date",
        "Supplier/Supplying Plant",
        "Short Text",
        "Purch. Organization",
        "Net Price",
        "Currency",
        "Net Order Value",
        "Purchasing Doc. Type",
        "Purch. Doc. Category",
        "Purchasing Group",
        "Material Group",
    )
    material_row = {
        "MaterialData Key": "MD-NEW-1",
        "Purchasing Document": "4500000001",
        "Item": "00010",
        "Plant": "1000",
        "Order Unit": "EA",
        "Document Date": "2025-01-01",
        "Supplier/Supplying Plant": "Supplier 1",
        "Short Text": "Synthetic consulting service",
        "Purch. Organization": "1000",
        "Net Price": "100.00",
        "Currency": "GBP",
        "Net Order Value": "100.00",
        "Purchasing Doc. Type": "NB",
        "Purch. Doc. Category": "F",
        "Purchasing Group": "A01",
        "Material Group": "SERVICES",
    }
    open_headers = (
        "Entity",
        "Document Number",
        "Line Number",
        "Document Type",
        "Document Date",
        "Posting Date",
        "Document Reference",
        "Document Currency Amount",
        "Document Currency",
        "Company Code Currency Amount",
        "Company Code Currency",
        "Group Currency Amount",
        "Group Currency",
        "GL Account",
        "GL Account Description",
        "Offsetting Account",
        "Vendor Number",
        "Vendor Name",
        "Purchasing Document",
        "Purchasing Document Item",
    )
    open_row = {
        "Entity": "SYN99",
        "Document Number": "900000001",
        "Line Number": "2",
        "Document Type": "KR",
        "Document Date": "2025-01-01",
        "Posting Date": "2025-01-02",
        "Document Reference": "SYNTHETIC-INV",
        "Document Currency Amount": "100.00",
        "Document Currency": "GBP",
        "Company Code Currency Amount": "100.00",
        "Company Code Currency": "GBP",
        "Group Currency Amount": "100.00",
        "Group Currency": "GBP",
        "GL Account": "210020",
        "GL Account Description": "Synthetic services",
        "Offsetting Account": "210000",
        "Vendor Number": "SUP-NEW",
        "Vendor Name": "Synthetic Supplier",
        "Purchasing Document": "4500000001",
        "Purchasing Document Item": "00010",
    }
    return (
        import_center_file("new-material.csv", material_headers, material_row),
        import_center_file("new-open-items.csv", open_headers, open_row),
        open_headers,
        open_row,
    )


@pytest.mark.django_db
def test_import_center_access_is_limited_to_administrators() -> None:
    client = pytest.importorskip("django.test").Client()
    assert client.get(reverse("import-center")).status_code == 302

    ordinary_user = User.objects.create_user(username="ordinary-import-user")
    client.force_login(ordinary_user)
    assert client.get(reverse("import-center")).status_code == 403

    administrator = import_center_admin()
    client.force_login(administrator)
    assert client.get(reverse("import-center")).status_code == 200


@pytest.mark.django_db
def test_import_center_preview_validates_csv_without_importing_business_data(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    material_file, open_file, _, _ = import_center_files()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        response = client.post(
            reverse("import-center"),
            {
                "material_data_file": material_file,
                "open_items_file": open_file,
            },
        )

    assert response.status_code == 302
    assert MaterialData.objects.count() == 0
    assert OpenItem.objects.count() == 0
    assert ImportBatch.objects.count() == 0
    upload = ImportUploadSession.objects.get()
    assert upload.status == ImportUploadSession.Status.READY
    assert upload.preview["material_matches"]["matched"] == 1
    assert upload.preview["material_matches"]["not_found"] == 0
    open_preview = next(
        file for file in upload.preview["files"] if file["kind"] == "open_items"
    )
    assert open_preview["rows"][0]["values"]["accounting_document_number"]
    assert open_preview["rows"][0]["outcome"] == "create"
    assert open_preview["rows"][0]["material_match"] == "Exactly one material match"
    preview_response = client.get(
        reverse("import-preview", args=[upload.token])
    )
    assert b"Validated row preview" in preview_response.content
    assert open_preview["rows"][0]["values"]["purchasing_document"].encode() in preview_response.content


@pytest.mark.django_db
def test_import_center_uses_last_material_duplicate_and_displays_warning(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    headers = (
        "MaterialData Key",
        "Purchasing Document",
        "Item",
        "Plant",
        "Order Unit",
        "Document Date",
        "Supplier/Supplying Plant",
        "Short Text",
        "Purch. Organization",
        "Net Price",
        "Currency",
        "Net Order Value",
        "Purchasing Doc. Type",
        "Purch. Doc. Category",
        "Purchasing Group",
        "Material Group",
    )
    first_row = {
        "MaterialData Key": "MD-NEW-1",
        "Purchasing Document": "4500000001",
        "Item": "00010",
        "Plant": "1000",
        "Order Unit": "EA",
        "Document Date": "2025-01-01",
        "Supplier/Supplying Plant": "Supplier 1",
        "Short Text": "Earlier description",
        "Purch. Organization": "1000",
        "Net Price": "100.00",
        "Currency": "GBP",
        "Net Order Value": "100.00",
        "Purchasing Doc. Type": "NB",
        "Purch. Doc. Category": "F",
        "Purchasing Group": "A01",
        "Material Group": "SERVICES",
    }
    last_row = {**first_row, "Short Text": "Final description"}
    upload_file = import_center_rows(
        "duplicate-material.csv",
        headers,
        [first_row, last_row],
    )

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        preview_response = client.post(
            reverse("import-center"),
            {"material_data_file": upload_file},
        )
        upload = ImportUploadSession.objects.get()
        preview = client.get(reverse("import-preview", args=[upload.token]))
        confirm_response = client.post(reverse("import-preview", args=[upload.token]))

    assert preview_response.status_code == 302
    assert upload.status == ImportUploadSession.Status.READY
    assert upload.preview["files"][0]["rows_created"] == 1
    assert upload.preview["files"][0]["rows_skipped"] == 1
    assert [row["outcome"] for row in upload.preview["files"][0]["rows"]] == [
        "skip",
        "create",
    ]
    assert "Earlier description" in upload.preview["files"][0]["rows"][0]["values"]["short_text"]
    assert "Final description" in upload.preview["files"][0]["rows"][1]["values"]["short_text"]
    assert "row 3 is used" in upload.preview["files"][0]["warnings"][0]["message"]
    assert b"Warnings" in preview.content
    assert b"row 3 is used" in preview.content
    assert confirm_response.status_code == 302
    assert MaterialData.objects.get().short_text == "Final description"


@pytest.mark.django_db
def test_import_center_imports_material_before_open_items_and_is_retry_safe(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    material_file, open_file, _, _ = import_center_files()

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        preview_response = client.post(
            reverse("import-center"),
            {
                "material_data_file": material_file,
                "open_items_file": open_file,
            },
        )
        upload = ImportUploadSession.objects.get()
        result_url = reverse("import-preview", args=[upload.token])
        confirm_response = client.post(result_url)
        result_page = client.get(result_url)

    assert preview_response.status_code == 302
    assert confirm_response.status_code == 302
    assert result_page.status_code == 200
    assert b"Created" in result_page.content
    item = OpenItem.objects.get(
        entity_code="SYN99",
        accounting_document_number="900000001",
        line_number="2",
    )
    assert item.material_matches.order_by("-created_at", "-id").first().status == "MATCHED"
    assert MaterialData.objects.count() == 1
    assert OpenItem.objects.count() == 1
    upload.refresh_from_db()
    assert upload.status == ImportUploadSession.Status.COMPLETED
    batches = ImportBatch.objects.filter(upload_session=upload).order_by("import_type")
    assert batches.count() == 2
    assert all(batch.uploaded_by == administrator for batch in batches)
    assert {batch.source_filename for batch in batches} == {
        "new-material.csv",
        "new-open-items.csv",
    }
    assert not list(tmp_path.glob("*.csv"))

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        retry_response = client.post(result_url)
    assert retry_response.status_code == 302
    assert MaterialData.objects.count() == 1
    assert OpenItem.objects.count() == 1
    assert ImportBatch.objects.count() == 2


@pytest.mark.django_db
def test_import_center_open_items_only_explains_unmatched_material_data(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    _, _, open_headers, open_row = import_center_files()
    open_row["Purchasing Document"] = "4500000099"
    open_file = import_center_file("open-only.csv", open_headers, open_row)

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        response = client.post(
            reverse("import-center"),
            {"open_items_file": open_file},
        )

    assert response.status_code == 302
    upload = ImportUploadSession.objects.get()
    assert "without MaterialData" in upload.preview["notice"]
    assert upload.preview["material_matches"]["not_found"] == 1
    assert MaterialData.objects.count() == 0
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_import_center_exposes_ambiguous_matches_instead_of_selecting_one(
    tmp_path,
    monkeypatch,
) -> None:
    class AmbiguousCandidates:
        def values(self, *fields):
            return self

        def annotate(self, **kwargs):
            return [
                {
                    "purchasing_document": "4500000001",
                    "item": "00010",
                    "candidate_count": 2,
                }
            ]

    monkeypatch.setattr(
        MaterialData.objects,
        "filter",
        lambda *args, **kwargs: AmbiguousCandidates(),
    )
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    _, _, open_headers, open_row = import_center_files()
    open_file = import_center_file("ambiguous.csv", open_headers, open_row)

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        response = client.post(
            reverse("import-center"),
            {"open_items_file": open_file},
        )

    assert response.status_code == 302
    upload = ImportUploadSession.objects.get()
    assert upload.preview["material_matches"]["ambiguous"] == 1
    assert upload.preview["material_matches"]["matched"] == 0
    open_preview = upload.preview["files"][0]
    assert open_preview["rows"][0]["material_match"].startswith("Ambiguous:")
    assert open_preview["rows"][0]["outcome"] == "create"
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_import_center_shows_duplicate_business_keys_and_disables_import(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    _, _, headers, row = import_center_files()
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerow(row)
    writer.writerow(row)
    upload_file = SimpleUploadedFile(
        "duplicate-open-items.csv",
        output.getvalue().encode("utf-8"),
        content_type="text/csv",
    )

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        response = client.post(
            reverse("import-center"),
            {"open_items_file": upload_file},
        )

    assert response.status_code == 302
    upload = ImportUploadSession.objects.get()
    assert upload.status == ImportUploadSession.Status.INVALID
    assert upload.preview["files"][0]["rows_rejected"] == 2
    assert len(upload.preview["files"][0]["errors"]) == 2
    assert [row["outcome"] for row in upload.preview["files"][0]["rows"]] == [
        "reject",
        "reject",
    ]
    assert all(row["reason"] for row in upload.preview["files"][0]["rows"])
    assert all(
        row["values"]["accounting_document_number"] == "900000001"
        for row in upload.preview["files"][0]["rows"]
    )
    preview_response = client.get(
        reverse("import-preview", args=[upload.token])
    )
    assert b"This complete upload was rejected" in preview_response.content
    assert b"Confirm and import" not in preview_response.content
    assert ImportBatch.objects.count() == 0
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_import_center_rejects_a_partially_invalid_batch_without_writes(
    tmp_path,
) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    _, _, headers, valid_row = import_center_files()
    invalid_row = dict(valid_row)
    invalid_row["Document Number"] = "900000002"
    invalid_row["Line Number"] = "3"
    invalid_row["Posting Date"] = "not-a-date"
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerow(valid_row)
    writer.writerow(invalid_row)
    open_file = SimpleUploadedFile(
        "partially-invalid.csv",
        output.getvalue().encode("utf-8"),
        content_type="text/csv",
    )

    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        client.post(reverse("import-center"), {"open_items_file": open_file})
        upload = ImportUploadSession.objects.get()
        preview_response = client.get(reverse("import-preview", args=[upload.token]))
        confirm_response = client.post(reverse("import-preview", args=[upload.token]))

    assert upload.status == ImportUploadSession.Status.INVALID
    assert upload.preview["can_import"] is False
    assert upload.preview["files"][0]["rows_rejected"] == 1
    assert "invalid Posting Date" in upload.preview["files"][0]["errors"][0]["message"]
    assert b"complete upload was rejected" in preview_response.content
    assert confirm_response.status_code == 302
    assert ImportBatch.objects.count() == 0
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_import_center_rejects_invalid_csv_and_oversize_upload(tmp_path) -> None:
    administrator = import_center_admin()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)

    invalid = SimpleUploadedFile(
        "not-a-csv.txt",
        b"not a csv",
        content_type="text/plain",
    )
    response = client.post(
        reverse("import-center"),
        {"open_items_file": invalid},
    )
    assert response.status_code == 200
    assert b"Upload a .csv file" in response.content

    missing_header = SimpleUploadedFile(
        "missing-columns.csv",
        b"Entity,Document Number\nSYN,1\n",
        content_type="text/csv",
    )
    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        response = client.post(
            reverse("import-center"),
            {"open_items_file": missing_header},
        )
    assert response.status_code == 200
    assert b"missing required columns" in response.content

    _, open_file, _, _ = import_center_files()
    with override_settings(
        WHT_IMPORT_MAX_FILE_SIZE=5,
        WHT_IMPORT_STAGING_DIR=tmp_path,
    ):
        response = client.post(
            reverse("import-center"),
            {"open_items_file": open_file},
        )
    assert response.status_code == 200
    assert b"configured upload-size limit" in response.content
    assert ImportBatch.objects.count() == 0
    assert OpenItem.objects.count() == 0


@pytest.mark.django_db
def test_import_center_failed_confirmation_rolls_back_all_business_changes(
    tmp_path,
    monkeypatch,
) -> None:
    from .services import import_center as import_service

    administrator = import_center_admin()
    material_file, open_file, _, _ = import_center_files()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)
    with override_settings(WHT_IMPORT_STAGING_DIR=tmp_path):
        client.post(
            reverse("import-center"),
            {
                "material_data_file": material_file,
                "open_items_file": open_file,
            },
        )
        upload = ImportUploadSession.objects.get()
        original_call_command = import_service.call_command

        def fail_after_material(command_name, *args, **kwargs):
            if command_name == "import_open_items":
                raise RuntimeError("synthetic failure")
            return original_call_command(command_name, *args, **kwargs)

        monkeypatch.setattr(import_service, "call_command", fail_after_material)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            import_service.confirm_import(upload, administrator)

    assert MaterialData.objects.count() == 0
    assert OpenItem.objects.count() == 0
    assert ImportBatch.objects.count() == 0
    upload.refresh_from_db()
    assert upload.status == ImportUploadSession.Status.READY


@pytest.mark.django_db
def test_import_center_cleans_expired_private_staging_files(tmp_path) -> None:
    from .services.import_center import cleanup_expired_uploads

    administrator = import_center_admin()
    staged_name = f"{uuid.uuid4().hex}.csv"
    staged_file = tmp_path / staged_name
    staged_file.write_text("synthetic staged file", encoding="utf-8")
    session = ImportUploadSession.objects.create(
        token=uuid.uuid4(),
        uploaded_by=administrator,
        material_staged_name=staged_name,
        material_original_name="expired.csv",
        material_sha256="0" * 64,
    )
    session.created_at = timezone.now() - timedelta(hours=2)
    session.save(update_fields=["created_at"])

    with override_settings(
        WHT_IMPORT_STAGING_DIR=tmp_path,
        WHT_IMPORT_STAGING_TTL_HOURS=1,
    ):
        assert cleanup_expired_uploads() == []

    session.refresh_from_db()
    assert session.status == ImportUploadSession.Status.EXPIRED
    assert not staged_file.exists()


@pytest.mark.django_db
def test_import_center_preserves_reference_data() -> None:
    administrator = import_center_admin()
    cost_type = CostType.objects.create(code="PERSISTENT", name="Persistent type")
    supplier = SupplierCountry.objects.create(
        supplier_id="PERSISTENT-SUPPLIER",
        country_code="GB",
        supplier_name="Persistent supplier",
    )
    mapping = WHTMapping.objects.create(
        supplier=supplier.supplier_id,
        supplier_name=supplier.supplier_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Persistent synthetic mapping",
    )
    user_count = User.objects.count()
    material_file, open_file, _, _ = import_center_files()
    client = pytest.importorskip("django.test").Client()
    client.force_login(administrator)

    preview_response = client.post(
        reverse("import-center"),
        {
            "material_data_file": material_file,
            "open_items_file": open_file,
        },
    )
    upload = ImportUploadSession.objects.get()
    client.post(reverse("import-preview", args=[upload.token]))

    assert preview_response.status_code == 302
    assert SupplierCountry.objects.get(pk=supplier.pk).country_code == "GB"
    assert WHTMapping.objects.get(pk=mapping.pk).wht_rate == Decimal("0.10")
    assert CostType.objects.get(pk=cost_type.pk).active
    assert User.objects.count() == user_count


@pytest.mark.django_db
def test_unauthenticated_users_cannot_access_invoice_data_or_exports() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008421",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number="600008421",
        gl_account="210020",
    )
    client = Client()
    urls = (
        reverse("dashboard"),
        reverse("open-item-list"),
        reverse("open-item-export"),
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        ),
        reverse("open-item-detail", args=[item.pk]),
        reverse("review-completed-items"),
        reverse("review-completed-export"),
    )

    for url in urls:
        response = client.get(url)
        assert response.status_code == 302, url
        assert response.url.startswith("/login/?next="), url


@pytest.mark.django_db
def test_transaction_users_can_view_and_download_active_and_completed_invoices() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008422",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number="600008422",
        gl_account="210020",
    )
    client = transaction_user_client()
    assert client.get(reverse("open-item-list"), {"status": "NEW"}).status_code == 200
    assert client.get(
        reverse("open-item-list"),
        {"status": "REVIEWED"},
    ).status_code == 200
    urls = (
        reverse("open-item-list"),
        reverse("open-item-export"),
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        ),
        reverse("open-item-detail", args=[item.pk]),
        reverse("review-completed-items"),
        reverse("review-completed-export"),
    )

    for url in urls:
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_active_and_completed_lists_follow_current_snapshot_and_reopening() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600009201",
        invoice_amount=Decimal("-100.00"),
        expense_amount=Decimal("100.00"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    transaction_client = transaction_user_client(
        username="invoice-list-transaction-user"
    )
    administrator = import_center_admin()
    administrator_client = Client()
    administrator_client.force_login(administrator)

    for client in (transaction_client, administrator_client):
        active = client.get(reverse("open-item-list"))
        completed = client.get(reverse("review-completed-items"))
        active_export = client.get(reverse("open-item-export"))
        completed_export = client.get(reverse("review-completed-export"))
        assert (
            active.status_code
            == completed.status_code
            == active_export.status_code
            == completed_export.status_code
            == 200
        )
        active_documents = {
            row["accounting_document_number"]
            for row in active.context["page_obj"].object_list
        }
        completed_documents = {
            row["accounting_document_number"]
            for row in completed.context["page_obj"].object_list
        }
        assert snapshot.accounting_document_number not in active_documents
        assert snapshot.accounting_document_number in completed_documents

    reopen_line(item, administrator)
    active = transaction_client.get(reverse("open-item-list"))
    completed = transaction_client.get(reverse("review-completed-items"))
    assert snapshot.accounting_document_number in {
        row["accounting_document_number"]
        for row in active.context["page_obj"].object_list
    }
    assert snapshot.accounting_document_number not in {
        row["accounting_document_number"]
        for row in completed.context["page_obj"].object_list
    }
    snapshot.refresh_from_db()
    assert snapshot.is_current is False
    history = transaction_client.get(
        reverse(
            "open-item-document",
            args=[snapshot.entity_code, snapshot.accounting_document_number],
        )
    )
    assert history.status_code == 200
    assert b"Historical" in history.content


@pytest.mark.django_db
def test_invoice_navigation_is_role_aware_and_exceptions_remain_admin_only() -> None:
    transaction_client = transaction_user_client(
        username="invoice-navigation-user"
    )
    administrator = import_center_admin()
    administrator_client = Client()
    administrator_client.force_login(administrator)

    transaction_navigation = transaction_client.get(reverse("open-item-list")).content
    admin_navigation = administrator_client.get(reverse("open-item-list")).content
    assert b"Active Invoices" in transaction_navigation
    assert b"Completed Invoices" in transaction_navigation
    assert b'href="/exceptions/"' not in transaction_navigation
    assert b"Active Invoices" in admin_navigation
    assert b"Completed Invoices" in admin_navigation
    assert b"Exceptions" in admin_navigation
    assert b"Import Center" not in transaction_navigation
    assert b"Import Center" in admin_navigation
    assert b"Django Admin" in admin_navigation
    assert transaction_client.get(reverse("exceptions-queue")).status_code == 403
    dashboard_cards = {
        label for label, _, _ in transaction_client.get(
            reverse("dashboard")
        ).context["cards"]
    }
    administrator_cards = {
        label for label, _, _ in administrator_client.get(
            reverse("dashboard")
        ).context["cards"]
    }
    assert dashboard_cards == {"Active Invoices", "Completed Invoices"}
    assert administrator_cards == dashboard_cards | {"Administrator Exceptions"}
    legacy_queue = transaction_client.get(
        reverse("open-item-list") + "?queue=classifier_work"
    )
    assert legacy_queue.status_code == 302
    assert legacy_queue.url == reverse("open-item-list")


@pytest.mark.django_db
def test_invoice_list_filters_match_export_without_recalculating_or_mutating() -> None:
    call_command("import_open_items")
    selected = OpenItem.objects.get(
        accounting_document_number="708000226",
        gl_account="210000",
    )
    before = {
        "determinations": WHTDetermination.objects.count(),
        "snapshots": ReviewDocumentSnapshot.objects.count(),
        "snapshot_values": list(
            ReviewDocumentSnapshot.objects.values_list(
                "document_amount",
                "calculated_wht",
                "booked_wht",
                "modification_required",
                "is_current",
            )
        ),
    }
    client = transaction_user_client(username="filtered-invoice-list-reader")
    params = {
        "entity": selected.entity_code,
        "supplier": selected.vendor_number,
        "document": selected.accounting_document_number,
        "reconciliation_status": "PENDING",
    }

    page = client.get(reverse("open-item-list"), params)
    export = client.get(reverse("open-item-export"), params)

    assert page.status_code == export.status_code == 200
    assert page.context["page_obj"].paginator.count == 1
    listed = page.context["page_obj"].object_list[0]
    workbook = load_workbook(BytesIO(export.content), data_only=True)
    sheet = workbook["Active Invoices"]
    assert sheet.max_row == 2
    assert tuple(sheet[1][index].value for index in range(len(HEADERS))) == tuple(
        HEADERS
    )
    assert sheet["B2"].value == listed["accounting_document_number"]
    assert sheet["C2"].value == listed["supplier"]
    assert sheet["P2"].value == listed["reconciliation_status"]
    assert page.context["filter_query"]
    assert WHTDetermination.objects.count() == before["determinations"]
    assert ReviewDocumentSnapshot.objects.count() == before["snapshots"]
    assert list(
        ReviewDocumentSnapshot.objects.values_list(
            "document_amount",
            "calculated_wht",
            "booked_wht",
            "modification_required",
            "is_current",
        )
    ) == before["snapshot_values"]


@pytest.mark.django_db
def test_transaction_user_can_complete_classification_review_and_completion() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = OpenItem.objects.get(
        entity_code="SYN01",
        accounting_document_number="708000226",
        line_number="2",
    )
    cost_type = CostType.objects.get(code="LABOR")
    supplier_line = document_supplier_line(item)
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier_line.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier_line.vendor_name,
        },
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.05"),
        country_code="GB",
        source_title="Transaction workflow test mapping",
    )
    client = transaction_user_client()

    response = client.post(
        reverse("open-item-detail", args=[item.pk]),
        {
            "final_cost_type": cost_type.pk,
            "classification_reason": "Reviewed against synthetic source data.",
        },
    )
    assert response.status_code == 302

    response = client.post(reverse("determine-wht", args=[item.pk]))
    assert response.status_code == 302
    response = client.post(reverse("complete-classification", args=[item.pk]))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == OpenItem.Status.COMPLETED
    invoice_lines = OpenItem.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    )
    non_classifiable_lines = list(invoice_lines.exclude(pk=item.pk))
    assert len(non_classifiable_lines) == 3
    assert all(not line.is_classifiable for line in non_classifiable_lines)
    assert all(line.status == OpenItem.Status.NEW for line in non_classifiable_lines)
    assert document_completion_blockers(item) == []

    active_response = client.get(reverse("open-item-list"))
    active_document = next(
        row
        for row in active_response.context["page_obj"].object_list
        if row["accounting_document_number"] == item.accounting_document_number
    )
    assert active_document["workflow_status"] == "Review pending"
    assert active_document["lines_requiring_work"] == 1
    completed_response = client.get(reverse("review-completed-items"))
    assert item.accounting_document_number not in {
        row["accounting_document_number"]
        for row in completed_response.context["page_obj"].object_list
    }
    detail_response = client.get(reverse("open-item-detail", args=[item.pk]))
    assert b"Submit for final review" not in detail_response.content
    assert b"Finalize invoice" in client.get(
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        )
    ).content
    response = client.post(reverse("finalize-invoice", args=[item.pk]))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == OpenItem.Status.REVIEWED
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    ).exists()
    snapshot = ReviewDocumentSnapshot.objects.get(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
        is_current=True,
    )
    assert snapshot.review_status == ReviewDocumentSnapshot.ReviewStatus.REVIEWED
    assert snapshot.calculated_wht == Decimal("53.25")
    assert snapshot.document_amount == Decimal("933.26")
    assert snapshot.booked_wht == Decimal("-62.45")
    assert snapshot.modification_required == Decimal("-9.20")
    completed_snapshot_values = (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
        snapshot.is_current,
    )

    active_response = client.get(reverse("open-item-list"))
    assert item.accounting_document_number not in {
        row["accounting_document_number"]
        for row in active_response.context["page_obj"].object_list
    }
    completed_response = client.get(reverse("review-completed-items"))
    assert item.accounting_document_number in {
        row["accounting_document_number"]
        for row in completed_response.context["page_obj"].object_list
    }
    snapshot.refresh_from_db()
    assert (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
        snapshot.is_current,
    ) == completed_snapshot_values
    duplicate_response = client.post(reverse("finalize-invoice", args=[item.pk]))
    assert duplicate_response.status_code == 302
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
        is_current=True,
    ).count() == 1


@pytest.mark.django_db
def test_unassigned_authenticated_user_cannot_read_or_post_workflow_routes() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    cost_type = CostType.objects.get(code="RENTAL")
    user = User.objects.create_user(username="unassigned-transaction-user")
    client = Client()
    client.force_login(user)

    read_urls = (
        reverse("dashboard"),
        reverse("open-item-list"),
        reverse("open-item-export"),
        reverse("open-item-document", args=[item.entity_code, item.accounting_document_number]),
        reverse("open-item-detail", args=[item.pk]),
    )
    for url in read_urls:
        assert client.get(url).status_code == 403, url

    post_data = {
        "final_cost_type": cost_type.pk,
        "classification_reason": "Unauthorized",
    }
    action_urls = (
        reverse("open-item-detail", args=[item.pk]),
        reverse("determine-wht", args=[item.pk]),
        reverse("complete-classification", args=[item.pk]),
        reverse("finalize-invoice", args=[item.pk]),
        reverse("submit-for-review", args=[item.pk]),
        reverse("start-review", args=[item.pk]),
        reverse("complete-review", args=[item.pk]),
        reverse("return-item", args=[item.pk]),
    )
    for url in action_urls:
        assert client.post(url, post_data).status_code == 403, url

    item.refresh_from_db()
    assert item.status == OpenItem.Status.NEW
    assert not item.classification_decisions.exists()
    assert not item.wht_determinations.exists()


@pytest.mark.django_db
def test_transaction_users_cannot_access_administrator_only_routes() -> None:
    client = transaction_user_client(is_staff=True)
    urls = (
        reverse("import-center"),
        reverse("import-preview", args=[uuid.uuid4()]),
        reverse("import-batch-detail", args=[1]),
        reverse("admin:transactions_materialdata_add"),
        reverse("admin:reference_data_costtype_add"),
        reverse("admin:reference_data_suppliercountry_add"),
        reverse("admin:reference_data_whtmapping_add"),
    )

    for url in urls:
        assert client.get(url).status_code == 403, url


@pytest.mark.django_db
def test_workflow_admin_records_are_read_only_and_audit_cannot_be_added() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.first()
    client = transaction_user_client(
        username="transaction-user-admin",
        is_staff=True,
    )
    user = User.objects.get(username="transaction-user-admin")
    request = RequestFactory().get("/admin/")
    request.user = user
    workflow_models = (
        OpenItem,
        MaterialMatch,
        ClassificationDecision,
        WHTDetermination,
        AuditEvent,
        ReviewAction,
    )

    for model in workflow_models:
        model_admin = admin.site._registry[model]
        assert model_admin.has_view_permission(request)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)

    assert ReviewDocumentSnapshot in admin.site._registry
    assert ReviewLineCalculationSnapshot in admin.site._registry
    assert User in admin.site._registry
    assert client.post(
        reverse("admin:transactions_openitem_change", args=[item.pk]),
        {"status": OpenItem.Status.REVIEWED},
    ).status_code == 403
    assert client.post(reverse("admin:transactions_auditevent_add")).status_code == 403
    item.refresh_from_db()
    assert item.status == OpenItem.Status.NEW


@pytest.mark.django_db
def test_completed_snapshot_history_is_visible_but_read_only_in_admin() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008420",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    line_snapshot = ReviewLineCalculationSnapshot.objects.get(
        document_snapshot=snapshot
    )
    client = transaction_user_client(
        username="transaction-user-snapshot-admin",
        is_staff=True,
    )
    request = RequestFactory().get("/admin/")
    request.user = User.objects.get(username="transaction-user-snapshot-admin")

    for model in (ReviewDocumentSnapshot, ReviewLineCalculationSnapshot):
        model_admin = admin.site._registry[model]
        assert model_admin.has_view_permission(request)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)

    document_history = client.get(
        reverse("admin:transactions_reviewdocumentsnapshot_changelist")
    )
    line_history = client.get(
        reverse("admin:transactions_reviewlinecalculationsnapshot_changelist")
    )
    assert document_history.status_code == 200
    assert b"600008420" in document_history.content
    assert line_history.status_code == 200
    assert str(line_snapshot.pk).encode() in line_history.content
    assert client.get(
        reverse(
            "admin:transactions_reviewdocumentsnapshot_change",
            args=[snapshot.pk],
        )
    ).status_code == 200
    assert client.post(
        reverse(
            "admin:transactions_reviewdocumentsnapshot_change",
            args=[snapshot.pk],
        ),
        {"is_current": "False"},
    ).status_code == 403


@pytest.mark.django_db
def test_transaction_users_cannot_change_wht_determinations_through_admin() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    determination = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="",
        wht_base_snapshot=item.document_currency_amount,
        currency_snapshot=item.document_currency,
    )
    client = transaction_user_client(
        username="transaction-user-wht-admin",
        is_staff=True,
    )

    response = client.post(
        reverse("admin:transactions_whtdetermination_change", args=[determination.pk]),
        {"status": WHTDetermination.Status.MATCHED},
    )

    assert response.status_code == 403
    determination.refresh_from_db()
    assert determination.status == WHTDetermination.Status.REQUIRES_REVIEW


@pytest.mark.django_db
def test_administrators_retain_reference_data_administration() -> None:
    administrator = import_center_admin()
    request = RequestFactory().get("/admin/")
    request.user = administrator

    for model in (CostType, SupplierCountry, WHTMapping, MaterialData):
        model_admin = admin.site._registry[model]
        assert model_admin.has_view_permission(request)
        assert model_admin.has_add_permission(request)
        assert model_admin.has_change_permission(request)
        assert model_admin.has_delete_permission(request)


@pytest.mark.django_db
def test_transaction_user_flags_line_and_administrator_resolves_it() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    transaction_client = transaction_user_client(
        username="line-flag-user",
        is_staff=True,
    )
    admin_client = Client()
    administrator = import_center_admin()
    admin_client.force_login(administrator)
    anonymous_client = Client()

    response = transaction_client.post(
        reverse("flag-line", args=[item.pk]),
        {"reason": "Please verify the purchasing support."},
    )
    assert response.status_code == 302
    record = LineExceptionRecord.objects.get(open_item=item)
    assert record.created_by.username == "line-flag-user"
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.ADMIN_REVIEW_FLAGGED,
    ).exists()
    assert transaction_client.post(
        reverse("flag-line", args=[item.pk]),
        {"reason": "Duplicate request"},
    ).status_code == 302
    assert LineExceptionRecord.objects.filter(
        open_item=item,
        issue_type=LineExceptionRecord.Type.USER_FLAG,
        status=LineExceptionRecord.Status.OPEN,
    ).count() == 1

    assert transaction_client.get(reverse("exceptions-queue")).status_code == 403
    assert anonymous_client.get(reverse("exceptions-queue")).status_code == 302
    assert anonymous_client.post(
        reverse("resolve-line-flag", args=[1]),
        {"action": "CLEAR", "decision": "No access"},
    ).status_code == 302
    assert admin_client.get(reverse("exceptions-queue")).status_code == 200
    assert b"Please verify the purchasing support." in admin_client.get(
        reverse("exceptions-queue")
    ).content
    assert transaction_client.post(
        reverse("resolve-line-flag", args=[record.pk]),
        {"action": "CLEAR", "decision": "Not sufficient"},
    ).status_code == 403
    assert transaction_client.post(
        reverse("exclude-line", args=[item.pk]),
        {"reason": "No permission"},
    ).status_code == 403

    assert admin_client.post(
        reverse("resolve-line-flag", args=[record.pk]),
        {"action": "CLEAR", "decision": "Source document reviewed."},
    ).status_code == 302
    record.refresh_from_db()
    assert record.status == LineExceptionRecord.Status.RESOLVED
    assert record.resolved_by == administrator
    assert record.resolved_at is not None
    assert record.reason == "Please verify the purchasing support."
    assert record.resolution_decision == "Source document reviewed."


@pytest.mark.django_db
def test_unresolved_flag_blocks_final_invoice_completion() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    item = classifiable_item()
    supplier = document_supplier_line(item)
    cost_type = CostType.objects.get(code="RENTAL")
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier.vendor_name,
        },
    )
    create_approved_wht_mapping(
        supplier=supplier.vendor_number,
        supplier_name=supplier.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Flag completion test mapping",
    )
    decision = ClassificationDecision.objects.create(
        open_item=item,
        final_cost_type=cost_type,
        classification_reason="Completion blocker test",
    )
    determine_wht(item, decision)
    item.status = OpenItem.Status.UNDER_REVIEW
    item.save(update_fields=["status"])
    requester, _ = User.objects.get_or_create(username="completion-flag-requester")
    transaction_group, _ = Group.objects.get_or_create(name="Transaction User")
    requester.groups.add(transaction_group)
    flag_line_for_review(item, requester, "Please verify this line.")

    with pytest.raises(ValueError, match="unresolved Administrator review flag"):
        complete_review(item, user=requester)
    with pytest.raises(ValueError, match="unresolved Administrator review flag"):
        finalize_invoice(item, requester)
    item.refresh_from_db()
    assert item.status == OpenItem.Status.UNDER_REVIEW
    assert not ReviewDocumentSnapshot.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
    ).exists()


@pytest.mark.django_db
def test_invoice_snapshot_is_created_only_after_all_required_lines_are_reviewed() -> None:
    call_command("import_open_items")
    call_command("import_reference_data")
    first = classifiable_item()
    second = OpenItem.objects.get(
        entity_code=first.entity_code,
        accounting_document_number=first.accounting_document_number,
        gl_account="210020",
    )
    second.pk = None
    second.line_number = "99"
    second.document_currency_amount = Decimal("25.00")
    second.company_code_currency_amount = Decimal("25.00")
    second.group_currency_amount = Decimal("25.00")
    second.status = OpenItem.Status.UNDER_REVIEW
    second.save()
    supplier = document_supplier_line(first)
    cost_type = CostType.objects.get(code="RENTAL")
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier.vendor_name,
        },
    )
    create_approved_wht_mapping(
        supplier=supplier.vendor_number,
        supplier_name=supplier.vendor_name,
        cost_category=cost_type,
        wht_rate=Decimal("0.10"),
        country_code="GB",
        source_title="Invoice completion test mapping",
    )
    for line in (first, second):
        ClassificationDecision.objects.create(
            open_item=line,
            final_cost_type=cost_type,
            classification_reason="Invoice completion test",
        )
        determine_wht(
            line,
            line.classification_decisions.get(is_current=True),
        )
        line.status = OpenItem.Status.UNDER_REVIEW
        line.save(update_fields=["status"])

    complete_review(first)
    assert not ReviewDocumentSnapshot.objects.filter(
        entity_code=first.entity_code,
        accounting_document_number=first.accounting_document_number,
    ).exists()

    complete_review(second)
    assert ReviewDocumentSnapshot.objects.filter(
        entity_code=first.entity_code,
        accounting_document_number=first.accounting_document_number,
        is_current=True,
    ).exists()


@pytest.mark.django_db
def test_completed_snapshot_cannot_be_changed_by_exception_workflow() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600008422",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    administrator = import_center_admin()
    transaction_user_client(username="completed-flag-requester")
    requester_user = User.objects.get(username="completed-flag-requester")
    before = (
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
        item.wht_determinations.count(),
    )

    with pytest.raises(ValueError, match="Completed invoices are immutable"):
        flag_line_for_review(item, requester_user, "Flag completed line.")
    with pytest.raises(ValueError, match="Completed invoices are immutable"):
        exclude_line(item, administrator, "Attempt to exclude completed line.")
    with pytest.raises(ValueError, match="Completed invoices are immutable"):
        recalculate_wht_exception(item, administrator)

    snapshot.refresh_from_db()
    assert (
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.review_status,
        snapshot.calculation_version,
        item.wht_determinations.count(),
    ) == before


@pytest.mark.django_db
def test_administrator_can_reopen_one_line_without_resetting_other_lines() -> None:
    first_snapshot = create_completed_snapshot_test_document(
        "600009101",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    first = OpenItem.objects.get(
        entity_code=first_snapshot.entity_code,
        accounting_document_number=first_snapshot.accounting_document_number,
        gl_account="210020",
    )
    second = OpenItem.objects.get(pk=first.pk)
    second.pk = None
    second.id = None
    second.line_number = "990"
    second.save()
    decision = first.classification_decisions.get(is_current=True)
    cloned_decision = ClassificationDecision.objects.create(
        open_item=second,
        final_cost_type=decision.final_cost_type,
        classification_reason="Preserved second-line decision",
    )
    determination = first.wht_determinations.first()
    determination.pk = None
    determination.id = None
    determination.open_item = second
    determination.save()
    completion_action = ReviewAction.objects.create(
        open_item=first,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
    )
    snapshot = create_review_document_snapshot(
        first,
        completion_action=completion_action,
    )
    prior_snapshot_values = (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    )
    first_decision_id = decision.pk
    second_decision_id = cloned_decision.pk
    administrator = import_center_admin()

    reopened = reopen_line(first, administrator)

    reopened.refresh_from_db()
    second.refresh_from_db()
    snapshot.refresh_from_db()
    assert reopened.status == OpenItem.Status.IN_CLASSIFICATION
    assert second.status == OpenItem.Status.REVIEWED
    assert reopened.classification_decisions.get(is_current=True).pk == first_decision_id
    assert second.classification_decisions.get(is_current=True).pk == second_decision_id
    assert reopened.wht_determinations.count() == 1
    assert snapshot.is_current is False
    assert (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    ) == prior_snapshot_values
    assert not completed_review_snapshots().filter(pk=snapshot.pk).exists()
    event = AuditEvent.objects.get(
        open_item=first,
        event_type=AuditEvent.EventType.LINE_REOPENED,
    )
    assert event.performed_by == administrator
    assert event.new_value["scope"] == "LINE"
    assert event.new_value["line_number"] == first.line_number
    with pytest.raises(ValueError, match="already active"):
        reopen_line(first, administrator)


@pytest.mark.django_db
def test_administrator_can_reopen_entire_invoice_and_preserve_snapshot_history() -> None:
    initial_snapshot = create_completed_snapshot_test_document(
        "600009102",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    first = OpenItem.objects.get(
        entity_code=initial_snapshot.entity_code,
        accounting_document_number=initial_snapshot.accounting_document_number,
        gl_account="210020",
    )
    second = OpenItem.objects.get(pk=first.pk)
    second.pk = None
    second.id = None
    second.line_number = "990"
    second.save()
    decision = first.classification_decisions.get(is_current=True)
    ClassificationDecision.objects.create(
        open_item=second,
        final_cost_type=decision.final_cost_type,
        classification_reason="Second required line",
    )
    determination = first.wht_determinations.first()
    determination.pk = None
    determination.id = None
    determination.open_item = second
    determination.save()
    completion_action = ReviewAction.objects.create(
        open_item=first,
        action_type=ReviewAction.ActionType.REVIEW_COMPLETED,
    )
    current_snapshot = create_review_document_snapshot(
        first,
        completion_action=completion_action,
    )
    snapshot_values = (
        current_snapshot.document_amount,
        current_snapshot.calculated_wht,
        current_snapshot.booked_wht,
        current_snapshot.modification_required,
    )
    administrator = import_center_admin()

    reopened_lines = reopen_invoice(first, administrator)

    first.refresh_from_db()
    second.refresh_from_db()
    current_snapshot.refresh_from_db()
    assert {line.pk for line in reopened_lines} == {first.pk, second.pk}
    assert first.status == OpenItem.Status.IN_CLASSIFICATION
    assert second.status == OpenItem.Status.IN_CLASSIFICATION
    assert first.classification_decisions.filter(is_current=True).exists()
    assert second.classification_decisions.filter(is_current=True).exists()
    assert not ReviewDocumentSnapshot.objects.filter(
        entity_code=first.entity_code,
        accounting_document_number=first.accounting_document_number,
        is_current=True,
    ).exists()
    assert current_snapshot.is_current is False
    assert (
        current_snapshot.document_amount,
        current_snapshot.calculated_wht,
        current_snapshot.booked_wht,
        current_snapshot.modification_required,
    ) == snapshot_values
    event = AuditEvent.objects.get(
        open_item=first,
        event_type=AuditEvent.EventType.INVOICE_REOPENED,
    )
    assert event.performed_by == administrator
    assert event.new_value["scope"] == "INVOICE"
    assert set(event.new_value["reopened_line_ids"]) == {first.pk, second.pk}
    exclude_line(second, administrator, "The second line is explicitly excluded.")
    complete_classification(first, administrator)
    submit_for_review(first, administrator)
    start_review(first, administrator)
    complete_review(first, administrator)
    recompleted = ReviewDocumentSnapshot.objects.get(
        entity_code=first.entity_code,
        accounting_document_number=first.accounting_document_number,
        is_current=True,
    )
    assert recompleted.calculation_version == current_snapshot.calculation_version + 1
    assert recompleted.modification_required == Decimal("105.49")
    assert second.exception_records.filter(
        status=LineExceptionRecord.Status.EXCLUDED
    ).exists()


@pytest.mark.django_db
def test_reopened_invoice_requires_administrator_and_post_routes() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600009103",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    user_client = transaction_user_client(
        username="transaction-user-reopen-denied",
        is_staff=True,
    )
    user = User.objects.get(username="transaction-user-reopen-denied")
    with pytest.raises(PermissionError, match="Only Administrators"):
        reopen_line(item, user)
    with pytest.raises(PermissionError, match="Only Administrators"):
        reopen_invoice(item, user)
    assert user_client.post(
        reverse("reopen-line", args=[item.pk]),
    ).status_code == 403
    assert user_client.post(
        reverse("reopen-invoice", args=[item.pk]),
    ).status_code == 403
    item.refresh_from_db()
    snapshot.refresh_from_db()
    assert item.status == OpenItem.Status.REVIEWED
    assert snapshot.is_current is True


@pytest.mark.django_db
def test_recompletion_creates_next_snapshot_and_preserves_prior_evidence() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600009104",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
        booked_amounts=(Decimal("-105.47"),),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    evidence = {
        "document_amount": snapshot.document_amount,
        "document_currency": snapshot.document_currency,
        "calculated_wht": snapshot.calculated_wht,
        "booked_wht": snapshot.booked_wht,
        "modification_required": snapshot.modification_required,
        "mapping_required": snapshot.mapping_required,
        "exception_reason": snapshot.exception_reason,
        "completed_at": snapshot.completed_at,
        "calculation_version": snapshot.calculation_version,
    }
    administrator = import_center_admin()

    item = reopen_line(item, administrator)
    item.refresh_from_db()
    complete_classification(item, administrator)
    submit_for_review(item, administrator)
    start_review(item, administrator)
    complete_review(item, administrator)

    snapshot.refresh_from_db()
    new_snapshot = ReviewDocumentSnapshot.objects.get(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
        is_current=True,
    )
    assert snapshot.is_current is False
    assert new_snapshot.pk != snapshot.pk
    assert new_snapshot.calculation_version == snapshot.calculation_version + 1
    assert new_snapshot.completed_by == administrator
    assert new_snapshot.completed_at is not None
    assert new_snapshot.modification_required == Decimal("0.02")
    assert {
        "document_amount": snapshot.document_amount,
        "document_currency": snapshot.document_currency,
        "calculated_wht": snapshot.calculated_wht,
        "booked_wht": snapshot.booked_wht,
        "modification_required": snapshot.modification_required,
        "mapping_required": snapshot.mapping_required,
        "exception_reason": snapshot.exception_reason,
        "completed_at": snapshot.completed_at,
        "calculation_version": snapshot.calculation_version,
    } == evidence
    history_client = Client()
    history_client.force_login(administrator)
    history_response = history_client.get(
        reverse(
            "open-item-document",
            args=[item.entity_code, item.accounting_document_number],
        )
    )
    assert history_response.status_code == 200
    assert b"Historical" in history_response.content
    assert b"Version" in history_response.content
    event = AuditEvent.objects.get(
        open_item=item,
        event_type=AuditEvent.EventType.INVOICE_RECOMPLETED,
    )
    assert event.performed_by == administrator
    assert event.new_value["snapshot_version"] == new_snapshot.calculation_version


@pytest.mark.django_db
def test_reopened_line_still_cannot_complete_with_unresolved_review_flag() -> None:
    snapshot = create_completed_snapshot_test_document(
        "600009105",
        invoice_amount=Decimal("-949.44"),
        expense_amount=Decimal("1054.90"),
        rate=Decimal("0.10"),
    )
    item = OpenItem.objects.get(
        entity_code=snapshot.entity_code,
        accounting_document_number=snapshot.accounting_document_number,
        gl_account="210020",
    )
    administrator = import_center_admin()
    user_client = transaction_user_client(
        username="reopened-flag-requester",
        is_staff=True,
    )
    requester = User.objects.get(username="reopened-flag-requester")

    item = reopen_line(item, administrator)
    flag_line_for_review(item, requester, "Recheck the supporting invoice.")
    complete_classification(item, administrator)
    item.refresh_from_db()
    submit_for_review(item, administrator)
    start_review(item, administrator)
    item.refresh_from_db()

    with pytest.raises(ValueError, match="unresolved Administrator review flag"):
        complete_review(item, administrator)

    item.refresh_from_db()
    snapshot.refresh_from_db()
    assert item.status == OpenItem.Status.UNDER_REVIEW
    assert snapshot.is_current is False
    assert not ReviewDocumentSnapshot.objects.filter(
        entity_code=item.entity_code,
        accounting_document_number=item.accounting_document_number,
        is_current=True,
    ).exists()


@pytest.mark.django_db
def test_admin_exclusion_is_reasoned_audited_and_not_a_completion_blocker() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    administrator = import_center_admin()
    client = Client()
    client.force_login(administrator)

    assert client.post(
        reverse("exclude-line", args=[item.pk]),
        {"reason": ""},
    ).status_code == 302
    assert not LineExceptionRecord.objects.filter(open_item=item).exists()
    assert client.post(
        reverse("exclude-line", args=[item.pk]),
        {"reason": "Duplicate accounting line; excluded after review."},
    ).status_code == 302
    item.refresh_from_db()
    record = LineExceptionRecord.objects.get(open_item=item)
    assert record.status == LineExceptionRecord.Status.EXCLUDED
    assert record.resolved_by == administrator
    assert record.resolution_decision == record.reason
    assert item.is_classifiable is False
    assert document_completion_blockers(item) == []
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.LINE_EXCLUDED,
    ).exists()


@pytest.mark.django_db
def test_wht_exception_resolution_is_admin_only_and_preserves_signed_amount() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    item.document_currency_amount = Decimal("-12.34")
    item.save(update_fields=["document_currency_amount"])
    cost_type = CostType.objects.create(code="ADMIN_ONE_OFF", name="Admin one-off")
    supplier = document_supplier_line(item)
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier.vendor_name,
        },
    )
    decision = save_final_classification(
        item,
        cost_type,
        "Exception workflow test",
    )
    missing_determination = determine_wht(item, decision)
    assert missing_determination.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert any(
        entry.open_item.pk == item.pk and entry.issue_type == "REQUIRES REVIEW"
        for entry in build_exception_queue()
    )
    ambiguous_item = OpenItem.objects.filter(
        gl_account="210020",
        document_currency_amount__gt=0,
    ).exclude(pk=item.pk).first()
    ambiguous_type = CostType.objects.create(
        code="AMBIGUOUS_WHT_TEST",
        name="Ambiguous WHT test",
    )
    ClassificationDecision.objects.create(
        open_item=ambiguous_item,
        final_cost_type=ambiguous_type,
        classification_reason="Ambiguous mapping regression test",
    )
    WHTDetermination.objects.create(
        open_item=ambiguous_item,
        status=WHTDetermination.Status.AMBIGUOUS,
        candidate_count=2,
        final_cost_type_snapshot=ambiguous_type.code,
        wht_base_snapshot=ambiguous_item.document_currency_amount,
        currency_snapshot=ambiguous_item.document_currency,
    )
    assert any(
        entry.open_item.pk == ambiguous_item.pk
        and entry.issue_type == "AMBIGUOUS"
        for entry in build_exception_queue()
    )
    ordinary_user = User.objects.create_user(username="wht-exception-transaction-user")
    administrator = import_center_admin()

    with pytest.raises(PermissionError, match="Only Administrators"):
        resolve_wht_exception(
            item,
            ordinary_user,
            "Not allowed",
            manual_rate=Decimal("0.10"),
        )
    with pytest.raises(ValueError, match="mandatory resolution reason"):
        resolve_wht_exception(
            item,
            administrator,
            "  ",
            manual_rate=Decimal("0.10"),
        )
    mapping_count = WHTMapping.objects.count()
    determination = resolve_wht_exception(
        item,
        administrator,
        "Approved transaction-specific rate after review.",
        manual_rate=Decimal("0.10"),
    )

    assert determination.status == WHTDetermination.Status.ADMIN_RESOLVED
    assert determination.rate_source == "MANUAL"
    assert determination.wht_base_snapshot == Decimal("-12.34")
    assert determination.wht_amount_snapshot == Decimal("-1.23")
    assert determination.manual_override_reason
    assert determination.calculated_by == administrator
    assert determination.calculated_at is not None
    assert has_valid_determination(
        determination,
        expected_cost_type=cost_type.code,
    )
    assert determination_status_label(determination) == "Resolved one-off override"
    assert determination.wht_amount_snapshot == (
        determination.wht_base_snapshot * determination.wht_rate_snapshot
    ).quantize(Decimal("0.01"))
    assert WHTMapping.objects.count() == mapping_count
    assert AuditEvent.objects.filter(
        open_item=item,
        event_type=AuditEvent.EventType.WHT_EXCEPTION_RESOLVED,
    ).exists()
    event = AuditEvent.objects.get(
        open_item=item,
        event_type=AuditEvent.EventType.WHT_EXCEPTION_RESOLVED,
    )
    assert event.previous_value["determination"] == missing_determination.pk
    assert event.new_value["determination"] == determination.pk
    assert event.new_value["status"] == WHTDetermination.Status.ADMIN_RESOLVED
    event_value = event.new_value
    event.new_value = {
        **event_value,
        "status": WHTDetermination.Status.REQUIRES_REVIEW,
    }
    event.save(update_fields=["new_value"])
    assert has_valid_determination(
        determination,
        expected_cost_type=cost_type.code,
    )
    event.new_value = event_value
    event.save(update_fields=["new_value"])
    event.new_value = {**event_value, "amount": "0.00"}
    event.save(update_fields=["new_value"])
    assert not has_valid_determination(
        determination,
        expected_cost_type=cost_type.code,
    )
    event.new_value = event_value
    event.save(update_fields=["new_value"])
    assert not any(
        entry.open_item.pk == item.pk
        for entry in build_exception_queue()
    )
    complete_classification(item, administrator)
    assert document_completion_blockers(item) == []
    transaction_client = transaction_user_client()
    snapshot = finalize_invoice(
        item,
        User.objects.get(username="transaction-user"),
    )
    assert snapshot.mapping_required is False
    assert snapshot.exception_reason == ""
    line_snapshot = snapshot.line_snapshots.get(open_item=item)
    assert line_snapshot.wht_determination == determination
    assert line_snapshot.wht_rate == Decimal("0.10")
    assert line_snapshot.calculated_line_wht == Decimal("-1.23")
    completed_row = next(
        row
        for row in transaction_client.get(
            reverse("review-completed-items")
        ).context["page_obj"].object_list
        if row["accounting_document_number"] == item.accounting_document_number
    )
    assert completed_row["completion_status"] == "Resolved"
    assert completed_row["outstanding_exception_count"] == 0
    completed_export = load_workbook(
        BytesIO(transaction_client.get(reverse("review-completed-export")).content),
        data_only=True,
    )["Completed Invoices"]
    exported = next(
        row
        for row in completed_export.iter_rows(min_row=2, values_only=True)
        if row[1] == item.accounting_document_number
    )
    assert exported[11] == 0
    assert exported[16] == "Resolved"

    original_snapshot_values = (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    )
    reopen_line(item, administrator)
    snapshot.refresh_from_db()
    assert snapshot.is_current is False
    assert (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    ) == original_snapshot_values
    item.refresh_from_db()
    replacement_cost_type = CostType.objects.create(
        code="ADMIN_ONE_OFF_RECLASSIFIED",
        name="Admin one-off reclassified",
    )
    save_final_classification(
        item,
        replacement_cost_type,
        "Reclassified after reopening.",
        administrator,
    )
    complete_classification(item, administrator)
    with pytest.raises(ValueError, match="WHT determination is stale"):
        finalize_invoice(item, User.objects.get(username="transaction-user"))
    replacement = resolve_wht_exception(
        item,
        administrator,
        "Re-approved one-off rate after reclassification.",
        manual_rate=Decimal("0.10"),
    )
    assert has_valid_determination(
        replacement,
        expected_cost_type=replacement_cost_type.code,
    )
    assert WHTMapping.objects.count() == mapping_count
    second_snapshot = finalize_invoice(
        item,
        User.objects.get(username="transaction-user"),
    )
    assert second_snapshot.calculation_version == 2
    assert not snapshot.is_current
    assert second_snapshot.line_snapshots.get(open_item=item).wht_determination == replacement
    with pytest.raises(ProtectedError):
        determination.delete()


@pytest.mark.django_db
def test_admin_no_wht_is_explicit_and_recalculation_does_not_rewrite_prior_results() -> None:
    call_command("import_open_items")
    item = classifiable_item()
    supplier_line = document_supplier_line(item)
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier_line.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier_line.vendor_name,
        },
    )
    cost_type = CostType.objects.create(code="ADMIN_NO_WHT", name="Admin No WHT")
    decision = save_final_classification(
        item,
        cost_type,
        "Exception workflow test",
    )
    prior = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot=cost_type.code,
        wht_base_snapshot=item.document_currency_amount,
        currency_snapshot=item.document_currency,
    )
    administrator = import_center_admin()
    mapping_count = WHTMapping.objects.count()
    with pytest.raises(ValueError, match="mandatory resolution reason"):
        resolve_wht_exception(
            item,
            administrator,
            " ",
            no_wht=True,
        )

    no_wht = resolve_wht_exception(
        item,
        administrator,
        "Administrator explicitly approves no withholding for this transaction.",
        no_wht=True,
    )
    assert no_wht.status == WHTDetermination.Status.NO_WHT
    assert no_wht.rate_source == "ADMIN_NO_WHT"
    assert no_wht.wht_rate_snapshot == Decimal("0")
    assert no_wht.wht_amount_snapshot == Decimal("0.00")
    assert no_wht.manual_override_reason
    assert has_valid_determination(
        no_wht,
        expected_cost_type=cost_type.code,
    )
    assert determination_status_label(no_wht) == "Resolved one-off No WHT"
    assert not any(
        entry.open_item.pk == item.pk
        for entry in build_exception_queue()
    )
    assert WHTMapping.objects.count() == mapping_count
    event = AuditEvent.objects.get(
        open_item=item,
        event_type=AuditEvent.EventType.WHT_EXCEPTION_RESOLVED,
        new_value__determination=no_wht.pk,
    )
    assert event.performed_by == administrator
    assert event.created_at >= no_wht.calculated_at
    assert event.comment == no_wht.manual_override_reason

    complete_classification(item, administrator)
    transaction_client = transaction_user_client(username="no-wht-finalizer")
    transaction_user = User.objects.get(username="no-wht-finalizer")
    snapshot = finalize_invoice(item, transaction_user)
    assert snapshot.mapping_required is False
    assert snapshot.exception_reason == ""
    line_snapshot = snapshot.line_snapshots.get(open_item=item)
    assert line_snapshot.wht_determination == no_wht
    assert line_snapshot.wht_rate == Decimal("0")
    assert line_snapshot.calculated_line_wht == Decimal("0.00")
    completed_row = next(
        row
        for row in transaction_client.get(
            reverse("review-completed-items")
        ).context["page_obj"].object_list
        if row["accounting_document_number"] == item.accounting_document_number
    )
    assert completed_row["completion_status"] == "Resolved"
    assert completed_row["outstanding_exception_count"] == 0
    exported_rows = load_workbook(
        BytesIO(transaction_client.get(reverse("review-completed-export")).content),
        data_only=True,
    )["Completed Invoices"]
    exported = next(
        row
        for row in exported_rows.iter_rows(min_row=2, values_only=True)
        if row[1] == item.accounting_document_number
    )
    assert exported[11] == 0
    assert exported[16] == "Resolved"

    snapshot_values = (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    )
    reopen_line(item, administrator)
    snapshot.refresh_from_db()
    assert snapshot.is_current is False
    assert (
        snapshot.document_amount,
        snapshot.calculated_wht,
        snapshot.booked_wht,
        snapshot.modification_required,
        snapshot.calculation_version,
    ) == snapshot_values

    item_to_recalculate = OpenItem.objects.filter(
        gl_account="210020",
        document_currency_amount__gt=0,
    ).exclude(pk=item.pk).first()
    supplier_line = document_supplier_line(item_to_recalculate)
    recalc_cost_type = CostType.objects.create(
        code="ADMIN_RECALC",
        name="Administrator recalculation",
    )
    recalc_decision = ClassificationDecision.objects.create(
        open_item=item_to_recalculate,
        final_cost_type=recalc_cost_type,
        classification_reason="Explicit recalculation test",
    )
    old_determination = WHTDetermination.objects.create(
        open_item=item_to_recalculate,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="STALE_COST_TYPE",
        wht_base_snapshot=item_to_recalculate.document_currency_amount,
        currency_snapshot=item_to_recalculate.document_currency,
    )
    assert any(
        entry.open_item.pk == item_to_recalculate.pk
        and entry.issue_type == "WHT STALE"
        for entry in build_exception_queue()
    )
    SupplierCountry.objects.update_or_create(
        supplier_id=supplier_line.vendor_number,
        defaults={
            "country_code": "GB",
            "supplier_name": supplier_line.vendor_name,
        },
    )
    create_approved_wht_mapping(
        supplier=supplier_line.vendor_number,
        supplier_name=supplier_line.vendor_name,
        cost_category=recalc_cost_type,
        wht_rate=Decimal("0.05"),
        country_code="GB",
        source_title="Approved corrected mapping",
    )
    item_to_recalculate.status = OpenItem.Status.UNDER_REVIEW
    item_to_recalculate.save(update_fields=["status"])
    recalculated = recalculate_wht_exception(item_to_recalculate, administrator)
    old_determination.refresh_from_db()
    assert recalculated.status == WHTDetermination.Status.MATCHED
    item_to_recalculate.refresh_from_db()
    assert item_to_recalculate.status == OpenItem.Status.COMPLETED
    assert old_determination.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert old_determination.pk != recalculated.pk
    assert recalc_decision.final_cost_type.code == recalculated.final_cost_type_snapshot
    assert AuditEvent.objects.filter(
        open_item=item_to_recalculate,
        event_type=AuditEvent.EventType.WHT_DETERMINATION_RECALCULATED,
    ).exists()
    submit_for_review(item_to_recalculate, administrator)
    start_review(item_to_recalculate, administrator)
    item_to_recalculate.refresh_from_db()
    assert item_to_recalculate.status == OpenItem.Status.UNDER_REVIEW
