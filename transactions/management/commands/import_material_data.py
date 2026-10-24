from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from transactions.models import ImportBatch, MaterialData, OpenItem
from transactions.services.csv_imports import keep_last_business_key_rows, read_csv_rows
from transactions.services.import_batches import finish_import_batch, get_import_batch
from transactions.services.material_matching import match_material


class Command(BaseCommand):
    help = "Import MaterialData records from a purchasing-data CSV file."

    required_fields = (
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

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "csv_file",
            nargs="?",
            default="data/wht_synthetic_data/material_data_synthetic.csv",
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

        parsed_rows, duplicate_warnings = keep_last_business_key_rows(
            parsed_rows,
            ("purchasing_document", "item"),
            "Purchasing Document + Item",
        )
        warnings.extend(duplicate_warnings)
        counts["skipped"] += len(duplicate_warnings)
        counts["rejected"] = len(errors)

        with transaction.atomic():
            batch = get_import_batch(path, ImportBatch.ImportType.MATERIAL_DATA)
            if not errors:
                for _, row_values in parsed_rows:
                    values = dict(row_values)
                    purchasing_document = str(values.pop("purchasing_document"))
                    item = str(values.pop("item"))
                    existing = MaterialData.objects.filter(
                        purchasing_document=purchasing_document,
                        item=item,
                    ).first()
                    if existing is None:
                        MaterialData.objects.create(
                            purchasing_document=purchasing_document,
                            item=item,
                            source_import_batch=batch,
                            **values,
                        )
                        counts["created"] += 1
                    elif all(
                        getattr(existing, field) == value
                        for field, value in values.items()
                    ):
                        counts["skipped"] += 1
                    else:
                        for field, value in values.items():
                            setattr(existing, field, value)
                        existing.source_import_batch = batch
                        existing.save()
                        counts["updated"] += 1
            finish_import_batch(
                batch,
                rows_read=counts["read"],
                rows_created=counts["created"],
                rows_updated=counts["updated"],
                rows_skipped=counts["skipped"],
                errors=errors,
            )

        if not errors:
            for item in OpenItem.objects.exclude(
                status__in=[OpenItem.Status.COMPLETED, OpenItem.Status.REVIEWED]
            ).iterator():
                match_material(item)

        for _, error in errors:
            self.stderr.write(self.style.WARNING(error))
        for _, warning in warnings:
            self.stderr.write(self.style.WARNING(warning))
        self.stdout.write(
            self.style.SUCCESS(
                "MaterialData: "
                + ", ".join(f"{name}={value}" for name, value in counts.items())
            )
        )

    def _parse_row(self, row: dict[str, str], row_number: int) -> dict[str, object]:
        missing = [field for field in self.required_fields if not (row.get(field) or "").strip()]
        if missing:
            raise ValueError(f"Rejected CSV row {row_number}: missing {', '.join(missing)}.")
        return {
            "material_data_key": self._text(row, "MaterialData Key"),
            "purchasing_document": self._text(row, "Purchasing Document"),
            "item": self._text(row, "Item"),
            "material": self._text(row, "Material"),
            "plant": self._text(row, "Plant"),
            "order_unit": self._text(row, "Order Unit"),
            "document_date": self._date(row, "Document Date", row_number),
            "supplier_or_supplying_plant": self._text(row, "Supplier/Supplying Plant"),
            "short_text": self._text(row, "Short Text"),
            "purchasing_organization": self._text(row, "Purch. Organization"),
            "net_price": self._decimal(row, "Net Price", row_number),
            "currency": self._text(row, "Currency"),
            "net_order_value": self._decimal(row, "Net Order Value", row_number),
            "purchasing_info_record": self._text(row, "Purchasing Info Rec."),
            "purchasing_document_type": self._text(row, "Purchasing Doc. Type"),
            "purchasing_document_category": self._text(row, "Purch. Doc. Category"),
            "purchasing_group": self._text(row, "Purchasing Group"),
            "material_group": self._text(row, "Material Group"),
        }

    def _text(self, row: dict[str, str], field: str) -> str:
        return (row.get(field) or "").strip()

    def _date(self, row: dict[str, str], field: str, row_number: int) -> date:
        try:
            return date.fromisoformat(self._text(row, field))
        except ValueError as error:
            raise ValueError(f"Rejected CSV row {row_number}: invalid {field}.") from error

    def _decimal(self, row: dict[str, str], field: str, row_number: int) -> Decimal:
        try:
            value = Decimal(self._text(row, field))
            if not value.is_finite():
                raise InvalidOperation
            model_field = (
                "net_price" if field == "Net Price" else "net_order_value"
            )
            MaterialData._meta.get_field(model_field).clean(value, None)
            return value
        except (InvalidOperation, ValueError, ValidationError) as error:
            raise ValueError(f"Rejected CSV row {row_number}: invalid {field}.") from error
