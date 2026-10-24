from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="OpenItem",
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
                ("title", models.CharField(max_length=100)),
                ("entity_code", models.CharField(max_length=50)),
                ("accounting_document_number", models.CharField(max_length=100)),
                ("line_number", models.CharField(max_length=50)),
                ("document_type", models.CharField(max_length=20)),
                ("document_date", models.DateField()),
                ("posting_date", models.DateField()),
                ("document_reference", models.CharField(max_length=100)),
                ("vendor_number", models.CharField(blank=True, max_length=100)),
                ("vendor_name", models.CharField(blank=True, max_length=200)),
                ("purchasing_document", models.CharField(blank=True, max_length=100)),
                ("purchasing_document_item", models.CharField(blank=True, max_length=50)),
                ("material_data_key", models.CharField(blank=True, max_length=150)),
                ("material_group", models.CharField(blank=True, max_length=100)),
                ("material", models.CharField(blank=True, max_length=150)),
                ("material_description", models.CharField(blank=True, max_length=255)),
                (
                    "document_currency_amount",
                    models.DecimalField(decimal_places=2, max_digits=18),
                ),
                ("document_currency", models.CharField(max_length=3)),
                (
                    "company_code_currency_amount",
                    models.DecimalField(decimal_places=2, max_digits=18),
                ),
                ("company_code_currency", models.CharField(max_length=3)),
                (
                    "group_currency_amount",
                    models.DecimalField(decimal_places=2, max_digits=18),
                ),
                ("group_currency", models.CharField(max_length=3)),
                ("gl_account", models.CharField(max_length=50)),
                ("gl_account_description", models.CharField(max_length=255)),
                ("offsetting_account", models.CharField(max_length=100)),
                ("text", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("NEW", "New")],
                        default="NEW",
                        max_length=30,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": [
                    "entity_code",
                    "accounting_document_number",
                    "line_number",
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="openitem",
            constraint=models.UniqueConstraint(
                fields=("entity_code", "accounting_document_number", "line_number"),
                name="unique_open_item_business_key",
            ),
        ),
    ]
