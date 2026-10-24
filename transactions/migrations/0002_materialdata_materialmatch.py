from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MaterialData",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("material_data_key", models.CharField(max_length=150)),
                ("purchasing_document", models.CharField(max_length=100)),
                ("item", models.CharField(max_length=50)),
                ("material", models.CharField(blank=True, max_length=150)),
                ("plant", models.CharField(max_length=50)),
                ("order_unit", models.CharField(max_length=20)),
                ("document_date", models.DateField()),
                ("supplier_or_supplying_plant", models.CharField(max_length=255)),
                ("short_text", models.TextField()),
                ("purchasing_organization", models.CharField(max_length=50)),
                ("net_price", models.DecimalField(decimal_places=2, max_digits=18)),
                ("currency", models.CharField(max_length=3)),
                ("net_order_value", models.DecimalField(decimal_places=2, max_digits=18)),
                ("purchasing_info_record", models.CharField(blank=True, max_length=100)),
                ("purchasing_document_type", models.CharField(max_length=20)),
                ("purchasing_document_category", models.CharField(max_length=20)),
                ("purchasing_group", models.CharField(max_length=50)),
                ("material_group", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["purchasing_document", "item", "id"]},
        ),
        migrations.CreateModel(
            name="MaterialMatch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("status", models.CharField(choices=[("NOT_FOUND", "Not found"), ("MATCHED", "Matched"), ("AMBIGUOUS", "Ambiguous")], max_length=20)),
                ("candidate_count", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "material_data",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="selected_matches",
                        to="transactions.materialdata",
                    ),
                ),
                (
                    "open_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="material_matches",
                        to="transactions.openitem",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
    ]
