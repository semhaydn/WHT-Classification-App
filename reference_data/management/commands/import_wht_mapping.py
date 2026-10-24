from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reference_data.models import CostType, WHTMapping
from transactions.services.csv_imports import read_csv_rows


class Command(BaseCommand):
    help = "Import draft WHT rules from the synthetic CSV file."
    required_fields = (
        "Supplier",
        "Supplier Name",
        "Cost Category",
        "WHT %",
        "Country",
        "Title",
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "csv_file",
            nargs="?",
            default="data/wht_synthetic_data/wht_mapping_synthetic.csv",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["csv_file"])
        if not path.exists():
            raise CommandError(f"CSV file does not exist: {path}")
        try:
            rows, errors = read_csv_rows(
                path.read_bytes(),
                path.name,
                self.required_fields,
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        rows_read = len(rows) + len(errors)

        parsed: list[tuple[int, dict[str, object]]] = []
        rows_by_key: dict[tuple[str, str, str], list[int]] = {}
        for row_number, row in rows:
            try:
                values = self._parse_row(row, row_number)
            except ValueError as error:
                errors.append((row_number, str(error)))
                continue
            key = (
                str(values["supplier"]),
                str(values["country_code"]),
                str(values["cost_category"]),
            )
            rows_by_key.setdefault(key, []).append(row_number)
            parsed.append((row_number, values))

        duplicate_rows = {
            row_number
            for row_numbers in rows_by_key.values()
            if len(row_numbers) > 1
            for row_number in row_numbers
        }
        for key, row_numbers in rows_by_key.items():
            if len(row_numbers) > 1:
                message = (
                    "Rejected duplicate Supplier + Country + Cost Category "
                    f"{key[0]} + {key[1]} + {key[2]} at CSV rows "
                    f"{', '.join(map(str, row_numbers))}."
                )
                errors.extend((row_number, message) for row_number in row_numbers)

        counts = {
            "read": rows_read,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "rejected": len(errors),
        }
        cost_types = {
            cost_type.code: cost_type
            for cost_type in CostType.objects.filter(
                code__in={str(values["cost_category"]) for _, values in parsed}
            )
        }
        rows_to_write = []
        for row_number, values in parsed:
            if row_number in duplicate_rows:
                continue
            cost_type = cost_types.get(str(values["cost_category"]))
            if cost_type is None:
                errors.append(
                    (
                        row_number,
                        f"Rejected CSV row {row_number}: unknown Cost Category.",
                    )
                )
                continue
            values = dict(values)
            values["cost_category"] = cost_type
            values["source_row_number"] = row_number
            lookup = {
                "supplier": values["supplier"],
                "country_code": values["country_code"],
                "cost_category": cost_type,
                "version": 1,
            }
            existing = WHTMapping.objects.filter(**lookup).first()
            if existing is not None:
                changed = any(
                    getattr(existing, field) != value
                    for field, value in values.items()
                )
                if changed and existing.status != WHTMapping.Status.DRAFT:
                    errors.append(
                        (
                            row_number,
                            f"Rejected CSV row {row_number}: approved WHT rules "
                            "cannot be changed by import.",
                        )
                    )
                    continue
                if not changed:
                    counts["skipped"] += 1
                    continue
                rows_to_write.append((existing, values))
            else:
                rows_to_write.append((None, values))

        counts["rejected"] = len(errors)
        if not errors:
            with transaction.atomic():
                for existing, values in rows_to_write:
                    if existing is None:
                        WHTMapping.objects.create(**values, version=1)
                        counts["created"] += 1
                    else:
                        for field, value in values.items():
                            setattr(existing, field, value)
                        existing.save()
                        counts["updated"] += 1

        for _, error in errors:
            self.stderr.write(self.style.WARNING(error))
        self.stdout.write(
            self.style.SUCCESS(
                "WHT rules (draft): "
                + ", ".join(f"{name}={value}" for name, value in counts.items())
            )
        )

    def _parse_row(self, row: dict[str, str], row_number: int) -> dict[str, object]:
        fields = (
            "Supplier",
            "Supplier Name",
            "Cost Category",
            "WHT %",
            "Country",
            "Title",
        )
        missing = [field for field in fields if not (row.get(field) or "").strip()]
        if missing:
            raise ValueError(
                f"Rejected CSV row {row_number}: missing {', '.join(missing)}."
            )
        try:
            rate = Decimal((row["WHT %"] or "").strip())
        except (InvalidOperation, ValueError) as error:
            raise ValueError(
                f"Rejected CSV row {row_number}: invalid WHT %."
            ) from error
        if not rate.is_finite():
            raise ValueError(
                f"Rejected CSV row {row_number}: invalid WHT %."
            )
        if rate < 0:
            raise ValueError(
                f"Rejected CSV row {row_number}: WHT % cannot be negative."
            )
        try:
            WHTMapping._meta.get_field("wht_rate").clean(rate, None)
        except ValidationError as error:
            raise ValueError(
                f"Rejected CSV row {row_number}: invalid WHT %."
            ) from error
        return {
            "supplier": row["Supplier"].strip(),
            "supplier_name": row["Supplier Name"].strip(),
            "cost_category": row["Cost Category"].strip().upper(),
            "wht_rate": rate,
            "country_code": row["Country"].strip().upper(),
            "source_title": row["Title"].strip(),
        }
