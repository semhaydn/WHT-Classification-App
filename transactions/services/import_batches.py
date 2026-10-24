import hashlib
from pathlib import Path

from django.utils import timezone

from transactions.models import ImportBatch, ImportValidationError


def get_import_batch(path: Path, import_type: str) -> ImportBatch:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    batch, _ = ImportBatch.objects.get_or_create(
        import_type=import_type,
        source_sha256=digest,
        defaults={"source_filename": path.name},
    )
    batch.source_filename = path.name
    batch.validation_errors.all().delete()
    return batch


def finish_import_batch(
    batch: ImportBatch,
    *,
    rows_read: int,
    rows_created: int,
    rows_updated: int,
    rows_skipped: int,
    errors: list[tuple[int, str]],
) -> None:
    batch.rows_read = rows_read
    batch.rows_created = rows_created
    batch.rows_updated = rows_updated
    batch.rows_skipped = rows_skipped
    batch.rows_rejected = len(errors)
    batch.completed_at = timezone.now()
    batch.save(
        update_fields=[
            "source_filename",
            "rows_read",
            "rows_created",
            "rows_updated",
            "rows_skipped",
            "rows_rejected",
            "completed_at",
        ]
    )
    ImportValidationError.objects.bulk_create(
        [
            ImportValidationError(
                batch=batch,
                row_number=row_number,
                message=message,
            )
            for row_number, message in errors
        ]
    )
