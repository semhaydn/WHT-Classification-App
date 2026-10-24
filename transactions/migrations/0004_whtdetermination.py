from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reference_data", "0002_whtmapping"),
        ("transactions", "0003_classificationdecision_auditevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="WHTDetermination",
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
                ("status", models.CharField(choices=[("MATCHED", "Matched"), ("NOT_FOUND", "Not found"), ("AMBIGUOUS", "Ambiguous"), ("REQUIRES_REVIEW", "Requires review")], max_length=20)),
                ("candidate_count", models.PositiveIntegerField(default=0)),
                ("final_cost_type_snapshot", models.CharField(max_length=100)),
                ("supplier_snapshot", models.CharField(blank=True, max_length=100)),
                ("supplier_country_snapshot", models.CharField(blank=True, max_length=2)),
                ("rule_snapshot", models.CharField(blank=True, max_length=200)),
                ("rule_version_snapshot", models.CharField(blank=True, max_length=100)),
                ("wht_rate_snapshot", models.DecimalField(blank=True, decimal_places=6, max_digits=7, null=True)),
                ("wht_base_snapshot", models.DecimalField(decimal_places=2, max_digits=18)),
                ("wht_amount_snapshot", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("currency_snapshot", models.CharField(max_length=3)),
                ("calculated_at", models.DateTimeField(auto_now_add=True)),
                (
                    "matched_mapping",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="determinations",
                        to="reference_data.whtmapping",
                    ),
                ),
                (
                    "open_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wht_determinations",
                        to="transactions.openitem",
                    ),
                ),
            ],
            options={"ordering": ["-calculated_at", "-id"]},
        ),
    ]
