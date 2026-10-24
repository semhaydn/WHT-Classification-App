import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.models import (
    AuditEvent,
    ClassificationDecision,
    HistoricalCostTypeSuggestion,
    ImportBatch,
    ImportUploadSession,
    ImportValidationError,
    MaterialData,
    MaterialMatch,
    OpenItem,
    ReviewAction,
    ReviewDocumentSnapshot,
    ReviewLineCalculationSnapshot,
    WHTDetermination,
)


RESET_MODELS = (
    ("OpenItem", OpenItem),
    ("MaterialData", MaterialData),
    ("MaterialMatch", MaterialMatch),
    ("ClassificationDecision", ClassificationDecision),
    ("ReviewAction", ReviewAction),
    ("AuditEvent", AuditEvent),
    ("WHTDetermination", WHTDetermination),
    ("ReviewDocumentSnapshot", ReviewDocumentSnapshot),
    ("ReviewLineCalculationSnapshot", ReviewLineCalculationSnapshot),
    ("HistoricalCostTypeSuggestion", HistoricalCostTypeSuggestion),
    ("ImportValidationError", ImportValidationError),
    ("ImportUploadSession", ImportUploadSession),
    ("ImportBatch", ImportBatch),
)


class Command(BaseCommand):
    help = (
        "Back up the SQLite database and remove only imported transaction/test "
        "data. Reference data, users, and permissions are preserved."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required to create a backup and delete imported test data.",
        )

    def handle(self, *args, **options) -> None:
        before = self._record_counts()
        self.stdout.write("Records before reset:")
        self._print_counts(before)
        if not options["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "No data was deleted. Re-run with --confirm to back up the "
                    "database and reset imported transaction data."
                )
            )
            self.stdout.write("Records after reset:")
            self._print_counts(self._record_counts())
            return

        batches = ImportBatch.objects.all()
        upload_sessions = ImportUploadSession.objects.all()
        if not batches.exists() and not upload_sessions.exists():
            self.stdout.write(
                self.style.WARNING("No tracked import batches exist; no data was deleted.")
            )
            self.stdout.write("Records after reset:")
            self._print_counts(self._record_counts())
            return

        backup_path = self._backup_database()
        batch_ids = list(batches.values_list("pk", flat=True))
        staged_names = [
            name
            for session in upload_sessions
            for name in (
                session.material_staged_name,
                session.open_items_staged_name,
            )
            if name
        ]
        with transaction.atomic():
            ReviewDocumentSnapshot.objects.filter(
                Q(source_import_batch_id__in=batch_ids)
                | Q(line_snapshots__source_import_batch_id__in=batch_ids)
                | Q(line_snapshots__open_item__source_import_batch_id__in=batch_ids)
            ).distinct().delete()
            MaterialMatch.objects.filter(
                Q(open_item__source_import_batch_id__in=batch_ids)
                | Q(material_data__source_import_batch_id__in=batch_ids)
            ).delete()
            deleted_upload_sessions, _ = upload_sessions.delete()
            deleted, _ = batches.delete()

        from transactions.services.import_center import remove_staged_files

        cleanup_errors = remove_staged_files(staged_names)
        self.stdout.write(self.style.SUCCESS(f"Database backup: {backup_path}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} records across tracked import batches and "
                f"{deleted_upload_sessions} upload-session records."
            )
        )
        if cleanup_errors:
            self.stderr.write(
                self.style.WARNING(
                    f"Could not remove {len(cleanup_errors)} staged upload file(s). "
                    "See server logs for details."
                )
            )
        self.stdout.write("Records after reset:")
        self._print_counts(self._record_counts())
        self.stdout.write("Preserved reference data and users:")
        self.stdout.write(
            f"  users={get_user_model().objects.count()} "
            f"cost_types={CostType.objects.count()} "
            f"supplier_countries={SupplierCountry.objects.count()} "
            f"wht_mappings={WHTMapping.objects.count()}"
        )

    def _backup_database(self) -> Path:
        if connection.vendor != "sqlite":
            raise CommandError(
                "Safe reset currently requires SQLite's online backup API; "
                "no records were deleted."
            )
        connection.ensure_connection()
        backup_dir = Path(
            getattr(
                settings,
                "WHT_RESET_BACKUP_DIR",
                settings.BASE_DIR / "backups",
            )
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        backup_path = backup_dir / f"wht-before-reset-{timestamp}.sqlite3"
        try:
            if connection.in_atomic_block:
                with sqlite3.connect(str(backup_path)) as target:
                    target.executescript(
                        "\n".join(connection.connection.iterdump())
                    )
            else:
                with sqlite3.connect(str(backup_path)) as target:
                    target.executescript(
                        "\n".join(connection.connection.iterdump())
                    )
            with sqlite3.connect(str(backup_path)) as verification:
                integrity = verification.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise sqlite3.DatabaseError(
                    "The generated backup did not pass SQLite integrity_check."
                )
        except (OSError, sqlite3.Error) as error:
            backup_path.unlink(missing_ok=True)
            raise CommandError(
                f"Database backup failed; no records were deleted: {error}"
            ) from error
        return backup_path

    def _record_counts(self) -> dict[str, int]:
        counts = {
            name: model.objects.count()
            for name, model in RESET_MODELS
        }
        counts.update(
            {
                "User": get_user_model().objects.count(),
                "Group": Group.objects.count(),
                "Permission": Permission.objects.count(),
                "CostType": CostType.objects.count(),
                "SupplierCountry": SupplierCountry.objects.count(),
                "WHTMapping": WHTMapping.objects.count(),
            }
        )
        return counts

    def _print_counts(self, counts: dict[str, int]) -> None:
        for name, count in counts.items():
            self.stdout.write(f"  {name}: {count}")
