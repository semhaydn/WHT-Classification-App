import csv
from pathlib import Path
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reference_data.models import CostType, SupplierCountry


class Command(BaseCommand):
    help = "Import CostType and SupplierCountry reference data from synthetic CSV files."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--countries",
            default="data/wht_synthetic_data/countries_synthetic.csv",
        )
        parser.add_argument(
            "--cost-types",
            "--cost-decisions",
            dest="cost_decisions",
            default="data/wht_synthetic_data/cost_types_synthetic.csv",
        )

    def handle(self, *args, **options) -> None:
        countries_path = self._path(options["countries"])
        cost_types_path = self._path(options["cost_decisions"])
        country_rows = self._read_csv(countries_path)
        cost_type_rows = self._read_csv(cost_types_path)

        with transaction.atomic():
            country_count, country_rejected = self._import_countries(country_rows)
            cost_type_count = self._import_cost_types(cost_type_rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {country_count} supplier countries and "
                f"{cost_type_count} cost types; rejected {country_rejected} "
                "invalid country rows."
            )
        )

    def _path(self, value: str) -> Path:
        path = Path(value)
        if not path.exists():
            raise CommandError(f"CSV file does not exist: {path}")
        return path

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames:
                raise CommandError(f"CSV file has no header: {path}")
            return [dict(row) for row in reader]

    def _import_countries(self, rows: Iterable[dict[str, str]]) -> tuple[int, int]:
        seen: set[str] = set()
        imported = 0
        rejected = 0
        for row_number, row in enumerate(rows, start=2):
            supplier_id = (row.get("Supplier") or "").strip()
            country_code = (row.get("Country") or "").strip()
            supplier_name = (row.get("Supplier Name") or "").strip()
            missing = [
                field
                for field, value in (
                    ("Supplier", supplier_id),
                    ("Country", country_code),
                    ("Supplier Name", supplier_name),
                )
                if not value
            ]
            if missing:
                self.stderr.write(
                    f"Rejected country CSV row {row_number}: missing "
                    f"{', '.join(missing)}."
                )
                rejected += 1
                continue
            self._reject_duplicate(seen, supplier_id, "Supplier", row_number)
            SupplierCountry.objects.update_or_create(
                supplier_id=supplier_id,
                defaults={
                    "country_code": country_code,
                    "supplier_name": supplier_name,
                },
            )
            imported += 1
        return imported, rejected

    def _import_cost_types(self, rows: Iterable[dict[str, str]]) -> int:
        codes = set()
        for row_number, row in enumerate(rows, start=2):
            value = (row.get("Cost Type") or "").strip().upper()
            if not value:
                raise CommandError(f"Missing Cost Type at CSV row {row_number}.")
            codes.add(value)
        if not codes:
            raise CommandError("Cost Type CSV contains no non-empty Cost Type values.")
        for code in sorted(codes):
            CostType.objects.update_or_create(
                code=code,
                defaults={"name": code},
            )
        return len(codes)

    def _reject_duplicate(
        self,
        seen: set[str],
        value: str,
        field: str,
        row_number: int,
    ) -> None:
        if value in seen:
            raise CommandError(
                f"Duplicate {field} value {value!r} at CSV row {row_number}."
            )
        seen.add(value)
