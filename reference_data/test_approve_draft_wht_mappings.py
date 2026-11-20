from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from reference_data.models import CostType, SupplierCountry, WHTMapping


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
        "expected_count": 47,
    }
    defaults.update(options)
    call_command("approve_draft_wht_mappings", **defaults)


@pytest.mark.django_db
def test_dry_run_does_not_mutate_mappings(capsys) -> None:
    administrator, _, _ = create_approval_fixture()
    original = list(WHTMapping.objects.order_by("pk").values())

    run_command(administrator)

    output = capsys.readouterr().out
    assert "Mapping counts: total=47 approved=0 draft=47" in output
    assert "Preflight passed: exactly 47 mappings would be approved." in output
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47
    assert list(WHTMapping.objects.order_by("pk").values()) == original


@pytest.mark.django_db
def test_confirm_approves_all_drafts(monkeypatch) -> None:
    administrator, _, mappings = create_approval_fixture()
    from reference_data.management.commands import approve_draft_wht_mappings

    original_approve = approve_draft_wht_mappings.approve_wht_rule
    approved_ids = []

    def record_approval(rule_id, user):
        approved_ids.append(rule_id)
        return original_approve(rule_id, user)

    monkeypatch.setattr(
        approve_draft_wht_mappings, "approve_wht_rule", record_approval
    )

    run_command(administrator, confirm=True)

    assert approved_ids == [rule.pk for rule in mappings]
    assert WHTMapping.objects.count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 0
    assert not WHTMapping.objects.exclude(
        effective_from=date(2024, 3, 1),
        effective_to=date(2027, 12, 1),
    ).exists()
    assert not WHTMapping.objects.exclude(approved_by=administrator).exists()
    assert not WHTMapping.objects.filter(approved_at__isnull=True).exists()


@pytest.mark.django_db
def test_fresh_synthetic_import_can_be_approved(tmp_path) -> None:
    administrator = get_user_model().objects.create_superuser(
        username="import-admin", password="synthetic-password"
    )
    call_command("import_reference_data")
    call_command("import_wht_mapping")

    assert WHTMapping.objects.count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47

    run_command(administrator, confirm=True)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).count() == 47
    assert not WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).exists()
    call_command("generate_uat_dataset", output_dir=str(tmp_path))
    assert (tmp_path / "uat_scenarios.csv").exists()


@pytest.mark.django_db
def test_approval_requires_administrator() -> None:
    administrator, reviewer, _ = create_approval_fixture()

    with pytest.raises(CommandError, match="not an authorized Administrator"):
        run_command(administrator, user_id=reviewer.pk, confirm=True)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


@pytest.mark.django_db
def test_expected_count_mismatch_aborts() -> None:
    administrator, _, _ = create_approval_fixture()

    with pytest.raises(CommandError, match="must be 47"):
        run_command(administrator, expected_count=43)

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


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

    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


@pytest.mark.django_db
def test_supplier_country_mismatch_blocks_entire_batch(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    mappings[3].supplier = "MISSING-SUPPLIER"
    mappings[3].save(update_fields=["supplier"])

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator, confirm=True)

    assert "does not exist in SupplierCountry" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["missing", "extra", "approved"])
def test_unexpected_mapping_counts_abort(change: str) -> None:
    administrator, _, mappings = create_approval_fixture()
    if change == "missing":
        mappings[0].delete()
    elif change == "extra":
        WHTMapping.objects.create(
            supplier=mappings[0].supplier,
            supplier_name=mappings[0].supplier_name,
            cost_category=mappings[0].cost_category,
            wht_rate=mappings[0].wht_rate,
            country_code=mappings[0].country_code,
            source_title="Extra mapping",
            version=2,
        )
    else:
        from reference_data.services import approve_wht_rule

        mappings[0].effective_from = date(2024, 3, 1)
        mappings[0].save(update_fields=["effective_from"])
        approve_wht_rule(mappings[0].pk, administrator)

    original = list(WHTMapping.objects.order_by("pk").values())
    with pytest.raises(CommandError, match="Mapping count check failed"):
        run_command(administrator, confirm=True)

    assert list(WHTMapping.objects.order_by("pk").values()) == original


@pytest.mark.django_db
def test_conflicting_draft_candidates_block_batch(capsys) -> None:
    administrator, _, _ = create_approval_fixture(conflicting_drafts=True)

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator, confirm=True)

    assert "conflicts with draft mapping ID" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


@pytest.mark.django_db
def test_country_disagreement_blocks_batch(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    mappings[0].country_code = "US"
    mappings[0].save(update_fields=["country_code"])

    with pytest.raises(CommandError, match="Preflight failed"):
        run_command(administrator, confirm=True)

    assert "does not match SupplierCountry country" in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


@pytest.mark.django_db
def test_inactive_cost_type_produces_warning(capsys) -> None:
    administrator, _, mappings = create_approval_fixture()
    cost_type = mappings[0].cost_category
    cost_type.active = False
    cost_type.save(update_fields=["active"])

    run_command(administrator)

    assert "WARNING: Cost Type BULK is inactive." in capsys.readouterr().err
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47


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

    assert calls == 2
    assert not WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).exists()
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47
    assert not WHTMapping.objects.filter(effective_from__isnull=False).exists()
    assert not WHTMapping.objects.filter(effective_to__isnull=False).exists()
    assert not WHTMapping.objects.filter(approved_by__isnull=False).exists()
    assert not WHTMapping.objects.filter(approved_at__isnull=False).exists()


@pytest.mark.django_db
def test_unexpected_final_counts_roll_back_batch(monkeypatch) -> None:
    administrator, _, mappings = create_approval_fixture()
    from reference_data.management.commands import approve_draft_wht_mappings

    original_approve = approve_draft_wht_mappings.approve_wht_rule
    calls = 0

    def add_unexpected_mapping(rule_id, user):
        nonlocal calls
        result = original_approve(rule_id, user)
        calls += 1
        if calls == 47:
            WHTMapping.objects.create(
                supplier=mappings[0].supplier,
                supplier_name=mappings[0].supplier_name,
                cost_category=mappings[0].cost_category,
                wht_rate=mappings[0].wht_rate,
                country_code=mappings[0].country_code,
                source_title="Unexpected mapping",
                version=2,
            )
        return result

    monkeypatch.setattr(
        approve_draft_wht_mappings, "approve_wht_rule", add_unexpected_mapping
    )

    with pytest.raises(CommandError, match="Final mapping counts"):
        run_command(administrator, confirm=True)

    assert calls == 47
    assert WHTMapping.objects.count() == 47
    assert WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count() == 47
    assert not WHTMapping.objects.filter(effective_from__isnull=False).exists()
    assert not WHTMapping.objects.filter(approved_at__isnull=False).exists()
