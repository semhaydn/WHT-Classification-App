import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from transactions.models import HistoricalCostTypeSuggestion, ImportBatch
from transactions.services.import_batches import finish_import_batch, get_import_batch


class Command(BaseCommand):
    help = (
        "Import historical Cost Type predictions as suggestions; "
        "they never become Final Cost Types."
    )

    required_fields = ("Document Number", "Item", "Cost Type")

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "csv_file",
            nargs="?",
            default="data/wht_synthetic_data/cost_type_decisions_synthetic.csv",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["csv_file"])
        if not path.exists():
            raise CommandError(f"CSV file does not exist: {path}")
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise CommandError(f"CSV file has no header: {path}")
            rows = list(reader)

        parsed: list[tuple[int, dict[str, object]]] = []
        errors: list[tuple[int, str]] = []
        for row_number, row in enumerate(rows, start=2):
            try:
                values = self._parse_row(row, row_number)
            except ValueError as error:
                errors.append((row_number, str(error)))
                continue
            parsed.append((row_number, values))

        counts = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
        }
        with transaction.atomic():
            batch = get_import_batch(
                path,
                ImportBatch.ImportType.HISTORICAL_COST_TYPES,
            )
            for row_number, values in parsed:
                suggestion, created = HistoricalCostTypeSuggestion.objects.get_or_create(
                    source_batch=batch,
                    source_row_number=row_number,
                    defaults=values,
                )
                if created:
                    counts["created"] += 1
                elif all(
                    getattr(suggestion, field) == value
                    for field, value in values.items()
                ):
                    counts["skipped"] += 1
                else:
                    for field, value in values.items():
                        setattr(suggestion, field, value)
                    suggestion.save()
                    counts["updated"] += 1

            finish_import_batch(
                batch,
                rows_read=len(rows),
                rows_created=counts["created"],
                rows_updated=counts["updated"],
                rows_skipped=counts["skipped"],
                errors=errors,
            )

        for _, error in errors:
            self.stderr.write(self.style.WARNING(error))
        self.stdout.write(
            self.style.SUCCESS(
                "Historical Cost Type suggestions: "
                f"read={len(rows)}, created={counts['created']}, "
                f"updated={counts['updated']}, skipped={counts['skipped']}, "
                f"rejected={len(errors)}. Imported values remain suggestions, "
                "not Final Cost Type decisions."
            )
        )

    def _parse_row(self, row: dict[str, str], row_number: int) -> dict[str, object]:
        missing = [
            field
            for field in self.required_fields
            if not (row.get(field) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"Rejected CSV row {row_number}: missing {', '.join(missing)}."
            )
        confidence_text = (row.get("Confidence Rate") or "").strip()
        try:
            confidence = Decimal(confidence_text) if confidence_text else None
        except (InvalidOperation, ValueError) as error:
            raise ValueError(
                f"Rejected CSV row {row_number}: invalid Confidence Rate."
            ) from error
        if confidence is not None and (
            not confidence.is_finite()
            or confidence < 0
            or confidence > 1
        ):
            raise ValueError(
                f"Rejected CSV row {row_number}: Confidence Rate must be between 0 and 1."
            )
        return {
            "source_document_number": row["Document Number"].strip(),
            "source_item_number": row["Item"].strip(),
            "suggested_cost_type": row["Cost Type"].strip(),
            "confidence_rate": confidence,
            "prediction_basis": (
                row.get("Prediction Basis")
                or row.get("PO Description")
                or ""
            ).strip(),
        }
