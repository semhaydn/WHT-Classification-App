from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import migrations, models


def mark_audited_manual_determinations_resolved(apps, schema_editor):
    WHTDetermination = apps.get_model("transactions", "WHTDetermination")
    AuditEvent = apps.get_model("transactions", "AuditEvent")
    database = schema_editor.connection.alias

    determinations = WHTDetermination.objects.using(database).filter(
        status="REQUIRES_REVIEW",
        rate_source="MANUAL",
    ).exclude(manual_override_reason="")
    for determination in determinations.iterator():
        if (
            not determination.calculated_by_id
            or not determination.supplier_snapshot
            or not determination.supplier_country_snapshot
            or determination.wht_rate_snapshot is None
            or determination.wht_rate_snapshot <= 0
            or not determination.wht_base_snapshot.is_finite()
        ):
            continue
        try:
            expected_amount = (
                determination.wht_base_snapshot * determination.wht_rate_snapshot
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError):
            continue
        if determination.wht_amount_snapshot != expected_amount:
            continue

        matching_audit = False
        events = AuditEvent.objects.using(database).filter(
            open_item_id=determination.open_item_id,
            event_type="WHT_EXCEPTION_RESOLVED",
            performed_by_id=determination.calculated_by_id,
            comment=determination.manual_override_reason,
            created_at__gte=determination.calculated_at,
        )
        for event in events.iterator():
            value = event.new_value or {}
            if not isinstance(value, dict):
                continue
            try:
                rate = Decimal(str(value["rate"]))
                amount = Decimal(str(value["amount"]))
            except (InvalidOperation, KeyError, TypeError):
                continue
            if (
                value.get("determination") == determination.pk
                and value.get("status") == "REQUIRES_REVIEW"
                and value.get("treatment") == "MANUAL_RATE"
                and rate == determination.wht_rate_snapshot
                and amount == determination.wht_amount_snapshot
            ):
                matching_audit = True
                break
        if matching_audit:
            WHTDetermination.objects.using(database).filter(
                pk=determination.pk
            ).update(status="ADMIN_RESOLVED")


class Migration(migrations.Migration):
    dependencies = [
        ("transactions", "0024_reviewlinecalculationsnapshot_wht_determination_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="whtdetermination",
            name="status",
            field=models.CharField(
                choices=[
                    ("MATCHED", "Matched"),
                    ("NO_WHT", "No WHT"),
                    ("NOT_FOUND", "Not found"),
                    ("AMBIGUOUS", "Ambiguous"),
                    ("REQUIRES_REVIEW", "Requires review"),
                    ("ADMIN_RESOLVED", "Resolved by Administrator"),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(mark_audited_manual_determinations_resolved),
    ]
