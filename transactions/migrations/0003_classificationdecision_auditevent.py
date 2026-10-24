from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reference_data", "0001_initial"),
        ("transactions", "0002_materialdata_materialmatch"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
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
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("CLASSIFICATION_SELECTED", "Classification selected"),
                            ("CLASSIFICATION_CHANGED", "Classification changed"),
                        ],
                        max_length=50,
                    ),
                ),
                ("previous_value", models.JSONField(blank=True, null=True)),
                ("new_value", models.JSONField(blank=True, null=True)),
                ("comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "open_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audit_events",
                        to="transactions.openitem",
                    ),
                ),
                (
                    "performed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wht_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ClassificationDecision",
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
                (
                    "classification_method",
                    models.CharField(
                        choices=[
                            ("HUMAN", "Human"),
                            ("HUMAN_OVERRIDE", "Human override"),
                            ("IMPORT", "Import"),
                            ("RULE", "Rule"),
                            ("ML_SUGGESTION", "ML suggestion"),
                        ],
                        default="HUMAN",
                        max_length=30,
                    ),
                ),
                ("classification_reason", models.TextField()),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                ("is_current", models.BooleanField(default=True)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="classification_decisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "final_cost_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="final_classifications",
                        to="reference_data.costtype",
                    ),
                ),
                (
                    "open_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="classification_decisions",
                        to="transactions.openitem",
                    ),
                ),
                (
                    "suggested_cost_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="suggested_classifications",
                        to="reference_data.costtype",
                    ),
                ),
            ],
            options={"ordering": ["-decided_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="classificationdecision",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_current", True)),
                fields=("open_item",),
                name="one_current_classification_per_item",
            ),
        ),
    ]
