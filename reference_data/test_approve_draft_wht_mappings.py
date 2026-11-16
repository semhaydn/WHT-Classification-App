from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from reference_data.models import CostType, SupplierCountry, WHTMapping
from reference_data.services import approve_wht_rule


EFFECTIVE_FROM = "2024-03-01"
EFFECTIVE_TO = "2027-12-01"


def create_approval_fixture(*, conflicting_drafts: bool = False):
    user_model = get_user_model()
    administrator = user_model.objects.create(
        username="bulk-admin",
        is_staff=True,
        is_superuser=True,
    )
    reviewer = user_model.objects.create_user(username="bulk-reviewer")
    cost_type = CostType.objects.create(code="BULK", name="Bulk approval")
    suppliers = [
        SupplierCountry.objects.create(
            supplier_id=f"SYN{index:06d}",
            country_code="GB",
            supplier_name=f"Synthetic supplier {index}",
        )
        for index in range(47)
    ]

    mappings = []
    for index, supplier in enumerate(suppliers):
        rule = WHTMapping.objects.create(
            supplier=supplier.supplier_id,
            supplier_name=supplier.supplier_name,
            cost_category=cost_type,
            wht_rate=Decimal("0.050000"),
            country_code=supplier.country_code,
            source_title=f"Rule {index}",
            version=1,
        )
        if index in {0, 1, 26}:
            rule.effective_from = date(2024, 3, 1)
            rule.effective_to = date(2027, 12, 1)
            rule.save(update_fields=["effective_from", "effective_to"])
            approve_wht_rule(rule.pk, administrator)
        mappings.append(rule)

    if conflicting_drafts:
        second = mappings[4]
        second.supplier = mappings[3].supplier
        second.version = 2
        second.save(update_fields=["supplier", "version"])

    return administrator, reviewer, mappings


def run_command(administrator, **options):
    defaults = {
        "user_id": administrator.pk,
        "effective_from": EFFECTIVE_FROM,
        "effective_to": EFFECTIVE_TO,
        "expected_count": 44,
    }
    defaults.update(options)
    call_command("approve_draft_wht_mappings", **defaults)


@pytest.mark.django_db
def test_dry_run_does_not_mutate_mappings(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    original_approved = list(
        WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED)
        .order_by("pk")
        .values(
            "id",
            "supplier",
            "country_code",
            "cost_category_id",
            "wht_rate",
            "source_title",
            "version",
            "effective_from",
            "effective_to",
            "status",
            "approved_by_id",
            "approved_at",
        )
    )

    run_command(administrator)

    output = capsys.readouterr().out
    assert "Mapping counts: total=47 approved=3 draft=44" in output
    assert "Preflight passed: exactly 44 mappings would be approved." in output
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44
    assert list(
        WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED)
        .order_by("pk")
        .values(
            "id",
            "supplier",
            "country_code",
            "cost_category_id",
            "wht_rate",
            "source_title",
            "version",
            "effective_from",
            "effective_to",
            "status",
            "approved_by_id",
            "approved_at",
        )
    ) == original_approved
    assert all(
        rule.effective_from is None and rule.effective_to is None
        for index, rule in enumerate(mappings)
        if index not in {0, 1, 26}
    )


@pytest.mark.django_db
def test_confirm_approves_drafts_and_preserves_existing_approvals() -> None:
    administrator, _, _ = create_approval_fixture()
    existing_approved = list(
        WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED)
        .order_by("pk")
        .values(
            "id",
            "supplier",
            "supplier_name",
            "cost_category_id",
            "wht_rate",
            "country_code",
            "source_title",
            "version",
            "effective_from",
            "effective_to",
            "status",
            "approved_by_id",
            "approved_at",
        )
    )

    run_command(administrator, confirm=True)

    assert WHTMapping.objects.count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 0
    assert not WHTMapping.objects.exclude(
        effective_from=date(2024, 3, 1),
        effective_to=date(2027, 12, 1),
    ).exists()
    assert list(
        WHTMapping.objects.filter(pk__in=[1, 2, 27])
        .order_by("pk")
        .values(
            "id",
            "supplier",
            "supplier_name",
            "cost_category_id",
            "wht_rate",
            "country_code",
            "source_title",
            "version",
            "effective_from",
            "effective_to",
            "status",
            "approved_by_id",
            "approved_at",
        )
    ) == existing_approved


@pytest.mark.django_db
def test_approval_requires_administrator() -> None:
    administrator, reviewer, _ = create_approval_fixture()

    with pytest.raises(CommandError, match="not an authorized Administrator"):
        run_command(administrator, user_id=reviewer.pk, confirm=True)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
def test_expected_count_mismatch_aborts() -> None:
    administrator, _, _ = create_approval_fixture()

    with pytest.raises(CommandError, match="must be 44"):
        run_command(administrator, expected_count=43)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("effective_from", "effective_to", "message"),
    [
        ("2024-02-30", EFFECTIVE_TO, "valid YYYY-MM-DD date"),
        (EFFECTIVE_TO, EFFECTIVE_FROM, "must not be earlier"),
    ],
)
def test_invalid_dates_abort(
    effective_from: str,
    effective_to: str,
    message: str,
) -> None:
    administrator, _, _ = create_approval_fixture()

    with pytest.raises(CommandError, match=message):
        run_command(
            administrator,
            effective_from=effective_from,
            effective_to=effective_to,
        )

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
def test_supplier_country_mismatch_blocks_entire_batch(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    mappings[3].supplier = "MISSING-SUPPLIER"
    mappings[3].save(update_fields=["supplier"])

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator)

    assert "does not exist in SupplierCountry" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
def test_overlap_with_existing_approved_mapping_blocks_batch(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    mappings[3].supplier = mappings[0].supplier
    mappings[3].version = 2
    mappings[3].save(update_fields=["supplier", "version"])

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator)

    assert "overlaps an approved rule" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
def test_conflicting_draft_candidates_block_batch(capsys) -> None:
    administrator, _, _ = create_approval_fixture(conflicting_drafts=True)

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator)

    assert "conflicts with draft mapping ID" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44


@pytest.mark.django_db
def test_approval_failure_rolls_back_entire_batch(monkeypatch) -> None:
    administrator, _, _ = create_approval_fixture()
    from reference_data.management.commands import approve_draft_wht_mappings

    original_approve = approve_draft_wht_mappings.approve_wht_rule
    calls = 0

    def fail_second_approval(rule_id, user):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic failure")
        return original_approve(rule_id, user)

    monkeypatch.setattr(
        approve_draft_wht_mappings,
        "approve_wht_rule",
        fail_second_approval,
    )

    with pytest.raises(CommandError, match="rolled back"):
        run_command(administrator, confirm=True)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).count() == 3
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 44
    assert not WHTMapping.objects.filter(
        status=WHTMapping.Status.DRAFT,
        effective_from__isnull=False,
    ).exists()
