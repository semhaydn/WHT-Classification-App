from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reference_data", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WHTMapping",
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
                ("supplier", models.CharField(max_length=100)),
                ("supplier_name", models.CharField(max_length=200)),
                ("wht_rate", models.DecimalField(decimal_places=6, max_digits=7)),
                ("country_code", models.CharField(max_length=2)),
                ("source_title", models.CharField(max_length=200)),
                ("source_row_number", models.PositiveIntegerField(unique=True)),
                (
                    "cost_category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wht_mappings",
                        to="reference_data.costtype",
                    ),
                ),
            ],
            options={"ordering": ["supplier", "cost_category__code", "country_code", "id"]},
        ),
    ]
