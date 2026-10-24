from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from transactions.models import ImportBatch, OpenItem
from transactions.services.import_batches import finish_import_batch, get_import_batch
from transactions.services.csv_imports import (
    read_csv_rows,
    reject_duplicate_business_keys,
)
from transactions.services.material_matching import match_material


class Command(BaseCommand):
    help = "Import OpenItem records from an SAP open-items CSV file."

    required_fields = (
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
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "csv_file",
            nargs="?",
            default="data/wht_synthetic_data/open_line_items_synthetic.csv",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["csv_file"])
        if not path.exists():
            self.stderr.write(self.style.ERROR(f"CSV file does not exist: {path}"))
            return

        try:
            rows, structure_errors = read_csv_rows(
                path.read_bytes(),
                path.name,
                self.required_fields,
            )
        except ValueError as error:
            raise CommandError(str(error)) from error

        counts = {
            "read": len(rows) + len(structure_errors),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "rejected": 0,
        }
        errors = list(structure_errors)
        warnings: list[tuple[int, str]] = []
        parsed_rows: list[tuple[int, dict[str, object]]] = []
        for row_number, row in rows:
            try:
                values = self._parse_row(row, row_number)
            except ValueError as error:
                errors.append((row_number, str(error)))
                continue
            parsed_rows.append((row_number, values))

        parsed_rows, duplicate_errors = reject_duplicate_business_keys(
            parsed_rows,
            (
                "entity_code",
                "accounting_document_number",
                "line_number",
            ),
            "Entity + Document Number + Line Number",
        )
        errors.extend(duplicate_errors)

        row_keys = [
            {
                "entity_code": values["entity_code"],
                "accounting_document_number": values["accounting_document_number"],
                "line_number": values["line_number"],
            }
            for _, values in parsed_rows
        ]
        existing_by_key = {}
        existing_query = Q()
        for key in row_keys:
            existing_query |= Q(**key)
        if existing_query:
            existing_by_key = {
                (item.entity_code, item.accounting_document_number, item.line_number): item
                for item in OpenItem.objects.filter(existing_query)
            }

        completed_documents = set(
            OpenItem.objects.filter(
                status__in=[OpenItem.Status.COMPLETED, OpenItem.Status.REVIEWED]
            )
            .values_list("entity_code", "accounting_document_number")
            .distinct()
        )
        rows_to_import = []
        created_items = []
        for row_number, values in parsed_rows:
            key = (
                values["entity_code"],
                values["accounting_document_number"],
                values["line_number"],
            )
            document_key = key[:2]
            existing = existing_by_key.get(key)
            if document_key in completed_documents:
                counts["skipped"] += 1
                warnings.append(
                    (
                        row_number,
                        f"Skipped completed SAP document {document_key[0]} + "
                        f"{document_key[1]} at CSV row {row_number}.",
                    )
                )
            elif existing is None:
                rows_to_import.append((key, values))
            elif all(
                getattr(existing, field) == value
                for field, value in values.items()
                if field not in {"entity_code", "accounting_document_number", "line_number"}
            ):
                counts["skipped"] += 1
            else:
                message = (
                    "Rejected conflicting source content for SAP line "
                    f"{key[0]} + {key[1]} + {key[2]} at CSV row {row_number}."
                )
                errors.append((row_number, message))

        counts["rejected"] = len(errors)
        batch = None
        with transaction.atomic():
            batch = get_import_batch(path, ImportBatch.ImportType.OPEN_ITEMS)
            if not errors:
                for key, values in rows_to_import:
                    item = OpenItem.objects.create(
                        entity_code=key[0],
                        accounting_document_number=key[1],
                        line_number=key[2],
                        **{
                            field: value
                            for field, value in values.items()
                            if field not in {
                                "entity_code",
                                "accounting_document_number",
                                "line_number",
                            }
                        },
                        source_import_batch=batch,
                    )
                    created_items.append(item)
                counts["created"] = len(created_items)
            finish_import_batch(
                batch,
                rows_read=counts["read"],
                rows_created=counts["created"],
                rows_updated=counts["updated"],
                rows_skipped=counts["skipped"],
                errors=errors,
            )

        for item in created_items:
            match_material(item)

        for _, error in errors:
            self.stderr.write(self.style.WARNING(error))
        for _, warning in warnings:
            self.stderr.write(self.style.WARNING(warning))
        self.stdout.write(
            self.style.SUCCESS(
                "Open Items: "
                + ", ".join(f"{name}={value}" for name, value in counts.items())
            )
        )

    def _parse_row(self, row: dict[str, str], row_number: int) -> dict[str, object]:
        missing = [field for field in self.required_fields if not (row.get(field) or "").strip()]
        if missing:
            raise ValueError(f"Rejected CSV row {row_number}: missing {', '.join(missing)}.")

        values: dict[str, object] = {
            "title": self._text(row, "Title"),
            "entity_code": self._text(row, "Entity"),
            "accounting_document_number": self._text(row, "Document Number"),
            "line_number": self._text(row, "Line Number"),
            "document_type": self._text(row, "Document Type"),
            "document_date": self._date(row, "Document Date", row_number),
            "posting_date": self._date(row, "Posting Date", row_number),
            "document_reference": self._text(row, "Document Reference"),
            "vendor_number": self._text(row, "Vendor Number"),
            "vendor_name": self._text(row, "Vendor Name"),
            "purchasing_document": self._text(row, "Purchasing Document"),
            "purchasing_document_item": self._text(row, "Purchasing Document Item"),
            "material_data_key": self._text(row, "MaterialData Key"),
            "material_group": self._text(row, "Material Group"),
            "material": self._text(row, "Material"),
            "material_description": self._text(row, "Material Description"),
            "document_currency_amount": self._decimal(row, "Document Currency Amount", row_number),
            "document_currency": self._text(row, "Document Currency"),
            "company_code_currency_amount": self._decimal(row, "Company Code Currency Amount", row_number),
            "company_code_currency": self._text(row, "Company Code Currency"),
            "group_currency_amount": self._decimal(row, "Group Currency Amount", row_number),
            "group_currency": self._text(row, "Group Currency"),
            "gl_account": self._text(row, "GL Account"),
            "gl_account_description": self._text(row, "GL Account Description"),
            "offsetting_account": self._text(row, "Offsetting Account"),
            "text": self._text(row, "Text"),
            "payment_block": self._text(row, "Payment Block"),
            "payment_method": self._text(row, "Payment Method"),
            "assignment": self._text(row, "Assignment"),
            "net_due_date": self._optional_date(row, "Net Due Date", row_number),
            "clearing_date": self._optional_date(row, "Clearing Date", row_number),
            "payment_date": self._optional_date(row, "Payment Date", row_number),
            "clearing_document": self._text(row, "Clearing Document"),
            "comments": self._text(row, "Comments"),
            "country": self._text(row, "Country").upper(),
        }
        return values

    def _text(self, row: dict[str, str], field: str) -> str:
        return (row.get(field) or "").strip()

    def _date(self, row: dict[str, str], field: str, row_number: int) -> date:
        try:
            return date.fromisoformat(self._text(row, field))
        except ValueError as error:
            raise ValueError(f"Rejected CSV row {row_number}: invalid {field}.") from error

    def _optional_date(self, row: dict[str, str], field: str, row_number: int) -> date | None:
        value = self._text(row, field)
        if not value:
            return None
        return self._date(row, field, row_number)

    def _decimal(self, row: dict[str, str], field: str, row_number: int) -> Decimal:
        try:
            value = Decimal(self._text(row, field))
            if not value.is_finite():
                raise InvalidOperation
            OpenItem._meta.get_field(
                {
                    "Document Currency Amount": "document_currency_amount",
                    "Company Code Currency Amount": "company_code_currency_amount",
                    "Group Currency Amount": "group_currency_amount",
                }[field]
            ).clean(value, None)
            return value
        except (InvalidOperation, ValueError, ValidationError) as error:
            raise ValueError(f"Rejected CSV row {row_number}: invalid {field}.") from error
