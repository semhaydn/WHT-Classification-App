import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from reference_data.models import WHTMapping
from transactions.models import (
    AuditEvent,
    OpenItem,
    ReviewDocumentSnapshot,
    ReviewLineCalculationSnapshot,
    WHTDetermination,
)


@pytest.mark.django_db(transaction=True)
def test_migration_0025_forward_is_selective_and_reverse_is_irreversible() -> None:
    call_command("import_open_items")
    item = OpenItem.objects.filter(gl_account="210020").first()
    assert item is not None
    actor = User.objects.create_user(username="migration-0025-admin")

    qualifying = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="SERVICES",
        supplier_snapshot="SUPPLIER-1",
        supplier_country_snapshot="GB",
        wht_rate_snapshot="0.05",
        wht_base_snapshot="-100.00",
        wht_amount_snapshot="-5.00",
        currency_snapshot="USD",
        rate_source="MANUAL",
        manual_override_reason="Audited one-off treatment",
        calculated_by=actor,
    )
    AuditEvent.objects.create(
        open_item=item,
        event_type="WHT_EXCEPTION_RESOLVED",
        performed_by=actor,
        comment=qualifying.manual_override_reason,
        new_value={
            "determination": qualifying.pk,
            "status": "REQUIRES_REVIEW",
            "rate": "0.05",
            "amount": "-5.00",
            "treatment": "MANUAL_RATE",
        },
    )
    unaudited = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="SERVICES",
        supplier_snapshot="SUPPLIER-1",
        supplier_country_snapshot="GB",
        wht_rate_snapshot="0.05",
        wht_base_snapshot="-100.00",
        wht_amount_snapshot="-5.00",
        currency_snapshot="USD",
        rate_source="MANUAL",
        manual_override_reason="No matching audit event",
        calculated_by=actor,
    )
    mapping_based = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.REQUIRES_REVIEW,
        final_cost_type_snapshot="SERVICES",
        supplier_snapshot="SUPPLIER-1",
        supplier_country_snapshot="GB",
        wht_base_snapshot="-100.00",
        currency_snapshot="USD",
        rate_source="MAPPING",
    )
    unrelated_resolved = WHTDetermination.objects.create(
        open_item=item,
        status=WHTDetermination.Status.ADMIN_RESOLVED,
        final_cost_type_snapshot="SERVICES",
        supplier_snapshot="SUPPLIER-1",
        supplier_country_snapshot="GB",
        wht_rate_snapshot="0.05",
        wht_base_snapshot="-100.00",
        wht_amount_snapshot="-5.00",
        currency_snapshot="USD",
        rate_source="MANUAL",
        manual_override_reason="Resolution created after migration",
        calculated_by=actor,
    )
    AuditEvent.objects.create(
        open_item=item,
        event_type="WHT_EXCEPTION_RESOLVED",
        performed_by=actor,
        comment=unrelated_resolved.manual_override_reason,
        new_value={
            "determination": unrelated_resolved.pk,
            "status": "ADMIN_RESOLVED",
            "rate": "0.05",
            "amount": "-5.00",
            "treatment": "MANUAL_RATE",
        },
    )
    snapshot = ReviewDocumentSnapshot.objects.create(
        entity_code="MIGRATION-TEST",
        accounting_document_number="MIGRATION-0025",
        document_amount="100.00",
        document_currency="USD",
        review_status=ReviewDocumentSnapshot.ReviewStatus.REVIEWED,
        completed_at=timezone.now(),
    )
    ReviewLineCalculationSnapshot.objects.create(
        document_snapshot=snapshot,
        open_item=item,
        line_number="test",
        eligible_service_amount="100.00",
        document_currency="USD",
        wht_determination=qualifying,
        wht_rate="0.05",
        calculated_line_wht="-5.00",
    )

    audit_before = list(AuditEvent.objects.order_by("pk").values())
    snapshots_before = list(ReviewDocumentSnapshot.objects.order_by("pk").values())
    line_snapshots_before = list(
        ReviewLineCalculationSnapshot.objects.order_by("pk").values()
    )
    mappings_before = list(WHTMapping.objects.order_by("pk").values())

    migration_module = importlib.import_module(
        "transactions.migrations.0025_whtdetermination_admin_resolved_status"
    )
    schema_editor = SimpleNamespace(connection=connection)
    migration_module.mark_audited_manual_determinations_resolved(
        apps, schema_editor
    )

    qualifying.refresh_from_db()
    unaudited.refresh_from_db()
    mapping_based.refresh_from_db()
    unrelated_resolved.refresh_from_db()
    assert qualifying.status == WHTDetermination.Status.ADMIN_RESOLVED
    assert unaudited.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert mapping_based.status == WHTDetermination.Status.REQUIRES_REVIEW
    assert unrelated_resolved.status == WHTDetermination.Status.ADMIN_RESOLVED

    migration = migration_module.Migration(
        "0025_whtdetermination_admin_resolved_status",
        "transactions",
    )
    project_state = MigrationLoader(connection).project_state(
        [("transactions", "0025_whtdetermination_admin_resolved_status")]
    )
    with connection.schema_editor() as schema_editor:
        with pytest.raises(IrreversibleError):
            migration.unapply(project_state, schema_editor)

    qualifying.refresh_from_db()
    unrelated_resolved.refresh_from_db()
    assert qualifying.status == WHTDetermination.Status.ADMIN_RESOLVED
    assert unrelated_resolved.status == WHTDetermination.Status.ADMIN_RESOLVED
    assert list(AuditEvent.objects.order_by("pk").values()) == audit_before
    assert list(ReviewDocumentSnapshot.objects.order_by("pk").values()) == snapshots_before
    assert (
        list(ReviewLineCalculationSnapshot.objects.order_by("pk").values())
        == line_snapshots_before
    )
    assert list(WHTMapping.objects.order_by("pk").values()) == mappings_before
