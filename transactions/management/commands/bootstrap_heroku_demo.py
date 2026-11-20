import csv
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.models import ImportBatch


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
        if marker.exists() and not database_path.exists():
            raise CommandError("Demo initialization marker exists but database is missing.")

        fixtures = settings.BASE_DIR / "data" / "wht_synthetic_data"
        paths = {
            name: fixtures / name
            for name in (
                "countries_synthetic.csv",
                "cost_types_synthetic.csv",
                "wht_mapping_synthetic.csv",
                "material_data_synthetic.csv",
                "cost_type_decisions_synthetic.csv",
                "open_line_items_synthetic.csv",
            )
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise CommandError(f"Missing synthetic fixtures: {', '.join(missing)}")

        call_command("migrate", interactive=False)
        with transaction.atomic():
            user_model = get_user_model()
            if marker.exists():
                user = user_model.objects.filter(username=username).first()
                if user and user.is_superuser:
                    self.stdout.write("Demo database already initialized.")
                    return
                if user_model.objects.exists():
                    raise CommandError("Demo database does not contain the configured superuser.")
            elif user_model.objects.exists():
                raise CommandError("Uninitialized demo database already contains users.")
            call_command(
                "import_reference_data",
                countries=str(paths["countries_synthetic.csv"]),
                cost_decisions=str(paths["cost_types_synthetic.csv"]),
            )
            call_command("import_wht_mapping", str(paths["wht_mapping_synthetic.csv"]))
            call_command("import_material_data", str(paths["material_data_synthetic.csv"]))
            call_command(
                "import_cost_type_decisions",
                str(paths["cost_type_decisions_synthetic.csv"]),
            )
            call_command("import_open_items", str(paths["open_line_items_synthetic.csv"]))
            with paths["wht_mapping_synthetic.csv"].open(
                newline="", encoding="utf-8-sig"
            ) as source:
                mapping_count = sum(1 for _ in csv.DictReader(source))
            if (
                not CostType.objects.exists()
                or not SupplierCountry.objects.exists()
                or not mapping_count
                or WHTMapping.objects.count() != mapping_count
                or ImportBatch.objects.count() != 3
                or ImportBatch.objects.filter(rows_read=0).exists()
                or ImportBatch.objects.filter(rows_rejected__gt=0).exists()
            ):
                raise CommandError("Synthetic fixture import was incomplete; initialization rolled back.")
            user_model.objects.create_superuser(username=username, password=password)
            marker.touch()

        self.stdout.write(self.style.SUCCESS("Disposable demo database initialized."))
