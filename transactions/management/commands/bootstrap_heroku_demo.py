import os
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.models import ImportBatch, MaterialData, OpenItem
from transactions.services.gl_eligibility import is_gl_account_eligible


MARKER_CONTENT = "uat-reference-v1\n"


class Command(BaseCommand):
    help = "Initialize the disposable Heroku demo database."

    def handle(self, *args, **options) -> None:
        if not settings.HEROKU_DEMO_MODE:
            raise CommandError("HEROKU_DEMO_MODE=true is required.")

        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not username or not password:
            raise CommandError(
                "DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD are required."
            )

        database_path = Path(settings.DATABASES["default"]["NAME"])
        marker = database_path.with_suffix(".initialized")
        if marker.exists():
            if not database_path.exists():
                raise CommandError(
                    "Demo initialization marker exists but database is missing."
                )
            if marker.read_text(encoding="utf-8") != MARKER_CONTENT:
                raise CommandError(
                    "Demo initialization marker is incompatible with UAT setup; "
                    "existing data was not changed."
                )
        elif database_path.exists():
            raise CommandError(
                "Unmarked demo database already exists; initialization refused. "
                "Existing data was not changed."
            )

        fixtures = settings.BASE_DIR / "data" / "wht_synthetic_data"
        paths = {
            name: fixtures / name
            for name in (
                "countries_synthetic.csv",
                "cost_types_synthetic.csv",
                "wht_mapping_synthetic.csv",
            )
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise CommandError(f"Missing synthetic fixtures: {', '.join(missing)}")

        if marker.exists():
            call_command("migrate", interactive=False)
            user = get_user_model().objects.filter(username=username).first()
            if not user or not user.is_superuser:
                raise CommandError("Demo database does not contain the configured superuser.")
            self.stdout.write("Demo database already initialized.")
            return

        call_command("migrate", interactive=False)
        with transaction.atomic():
            user = get_user_model().objects.create_superuser(
                username=username, password=password
            )
            call_command(
                "import_reference_data",
                countries=str(paths["countries_synthetic.csv"]),
                cost_decisions=str(paths["cost_types_synthetic.csv"]),
            )
            call_command("import_wht_mapping", str(paths["wht_mapping_synthetic.csv"]))
            call_command(
                "approve_draft_wht_mappings",
                user_id=user.pk,
                effective_from="2024-03-01",
                effective_to="2027-12-01",
                expected_count=47,
                confirm=True,
            )
            if (
                CostType.objects.count() != 13
                or CostType.objects.filter(active=True).count() != 13
                or SupplierCountry.objects.count() != 2323
                or WHTMapping.objects.count() != 47
                or WHTMapping.objects.filter(
                    status=WHTMapping.Status.APPROVED,
                    effective_from=date(2024, 3, 1),
                    effective_to=date(2027, 12, 1),
                    approved_by=user,
                    approved_at__isnull=False,
                ).count() != 47
                or MaterialData.objects.exists()
                or OpenItem.objects.exists()
                or ImportBatch.objects.exists()
                or is_gl_account_eligible("210000")
                or is_gl_account_eligible("229220")
                or not is_gl_account_eligible("210020")
                or not is_gl_account_eligible("570010")
            ):
                raise CommandError(
                    "Synthetic UAT reference state is incomplete; "
                    "initialization rolled back."
                )

        with marker.open("x", encoding="utf-8", newline="\n") as marker_file:
            marker_file.write(MARKER_CONTENT)

        self.stdout.write(self.style.SUCCESS("Disposable demo database initialized."))
