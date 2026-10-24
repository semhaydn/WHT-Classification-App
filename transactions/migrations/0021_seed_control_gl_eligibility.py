from django.db import migrations


def add_control_account_exclusions(apps, schema_editor):
    GLEligibility = apps.get_model("transactions", "GLEligibility")
    database = schema_editor.connection.alias
    configurations = (
        (
            "210000",
            "Invoice/control posting; excluded from classification.",
        ),
        (
            "229220",
            "Posted WHT control posting; excluded from classification.",
        ),
    )
    for gl_account, description in configurations:
        GLEligibility.objects.using(database).get_or_create(
            gl_account=gl_account,
            defaults={
                "is_eligible": False,
                "description": description,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0020_gleligibility"),
    ]

    operations = [
        migrations.RunPython(
            add_control_account_exclusions,
            migrations.RunPython.noop,
        ),
    ]
