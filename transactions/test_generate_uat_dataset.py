import csv
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.management.commands.generate_uat_dataset import (
    Command,
    MANIFEST_KEY_FIELDS,
    MATERIAL_DATA_HEADERS,
    OPEN_ITEM_HEADERS,
    OUTPUT_FILES,
    SCENARIO_HEADERS,
)
from transactions.models import GLEligibility, ImportBatch, MaterialData, OpenItem


ROOT = Path(__file__).resolve().parents[1]


def create_generator_reference_data(*, mapping_count: int = 47) -> None:
    administrator = get_user_model().objects.create_user(
        username="uat-generator-admin"
    )
    cost_codes = ("REPAIR", "LABOR", "RENTAL", "INTEREST", "ROYALTY")
    cost_types = [
        CostType.objects.create(
            code=cost_codes[index] if index < len(cost_codes) else f"OTHER{index:02d}",
            name=f"Synthetic cost type {index}",
        )
        for index in range(13)
    ]
    suppliers = SupplierCountry.objects.bulk_create(
        [
            SupplierCountry(
                supplier_id=f"VEN{index:06d}",
                country_code="GB",
                supplier_name=f"Synthetic supplier {index}",
            )
            for index in range(2323)
        ]
    )
    now = timezone.now()
    WHTMapping.objects.bulk_create(
        [
            WHTMapping(
                supplier=suppliers[index].supplier_id,
                supplier_name=suppliers[index].supplier_name,
                cost_category=cost_types[index % len(cost_types)],
                wht_rate=(
                    Decimal("0.000000")
                    if index == 0
                    else Decimal(("0.05", "0.10", "0.15")[index % 3])
                ),
                country_code="GB",
                source_title=f"Approved synthetic mapping {index}",
                version=1,
                effective_from=date(2024, 3, 1),
                effective_to=date(2027, 12, 1),
                status=WHTMapping.Status.APPROVED,
                approved_by=administrator,
                approved_at=now,
            )
            for index in range(mapping_count)
        ]
    )
    for account in ("210000", "229220"):
        GLEligibility.objects.get_or_create(
            gl_account=account,
            defaults={"is_eligible": False},
        )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or []), list(reader)


def reference_snapshot() -> tuple[object, ...]:
    return (
        list(
            SupplierCountry.objects.order_by("supplier_id").values_list(
                "supplier_id", "country_code", "supplier_name"
            )
        ),
        list(
            CostType.objects.order_by("pk").values_list(
                "pk", "code", "active", "name"
            )
        ),
        list(
            WHTMapping.objects.order_by("pk").values_list(
                "pk",
                "supplier",
                "country_code",
                "cost_category_id",
                "wht_rate",
                "version",
                "effective_from",
                "effective_to",
                "status",
                "approved_by_id",
                "approved_at",
            )
        ),
        list(GLEligibility.objects.order_by("gl_account").values_list(
            "gl_account", "is_eligible"
        )),
        OpenItem.objects.count(),
        MaterialData.objects.count(),
        ImportBatch.objects.count(),
    )


@pytest.mark.django_db
def test_generator_creates_valid_read_only_dataset(tmp_path: Path) -> None:
    create_generator_reference_data()
    before = reference_snapshot()
    output_dir = tmp_path / "realistic_v1"

    call_command("generate_uat_dataset", output_dir=str(output_dir))

    assert reference_snapshot() == before
    assert {path.name for path in output_dir.iterdir()} == set(OUTPUT_FILES)
    repeat_dir = tmp_path / "repeat"
    call_command("generate_uat_dataset", output_dir=str(repeat_dir))
    for filename in (
        "open_line_items_uat.csv",
        "material_data_uat.csv",
        "uat_scenarios.csv",
        "README.md",
    ):
        assert (repeat_dir / filename).read_bytes() == (
            output_dir / filename
        ).read_bytes()
    first_metadata = json.loads(
        (output_dir / "metadata.json").read_text(encoding="utf-8")
    )
    repeat_metadata = json.loads(
        (repeat_dir / "metadata.json").read_text(encoding="utf-8")
    )
    first_metadata.pop("generation_timestamp_utc")
    repeat_metadata.pop("generation_timestamp_utc")
    assert repeat_metadata == first_metadata

    open_headers, open_rows = read_csv(output_dir / "open_line_items_uat.csv")
    material_headers, material_rows = read_csv(
        output_dir / "material_data_uat.csv"
    )
    scenario_headers, scenarios = read_csv(output_dir / "uat_scenarios.csv")
    assert tuple(open_headers) == OPEN_ITEM_HEADERS
    assert tuple(material_headers) == MATERIAL_DATA_HEADERS
    assert tuple(scenario_headers) == SCENARIO_HEADERS

    with (ROOT / "data/wht_synthetic_data/open_line_items_synthetic.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        assert tuple(next(csv.reader(source))) == OPEN_ITEM_HEADERS
    with (ROOT / "data/wht_synthetic_data/material_data_synthetic.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        assert tuple(next(csv.reader(source))) == MATERIAL_DATA_HEADERS

    assert len(open_rows) == 353
    assert len(material_rows) == 152
    assert len(scenarios) == len(open_rows)
    source_keys = [
        tuple(row[field] for field in MANIFEST_KEY_FIELDS) for row in open_rows
    ]
    scenario_keys = [
        tuple(row[field] for field in MANIFEST_KEY_FIELDS) for row in scenarios
    ]
    assert all(all(key) for key in scenario_keys)
    assert len(set(source_keys)) == len(set(scenario_keys)) == 353
    assert set(source_keys) == set(scenario_keys)

    invoice_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in scenarios:
        invoice_rows[row["Invoice Number"]].append(row)
    assert len(invoice_rows) == 100
    assert Counter(len([
        row for row in rows if row["Line Type"] == "ELIGIBLE_EXPENSE"
    ]) for rows in invoice_rows.values()) == Counter({1: 60, 2: 30, 4: 10})

    treatment_counts = Counter(
        rows[0]["Expected WHT Treatment"] for rows in invoice_rows.values()
    )
    assert treatment_counts == {
        "POSITIVE_RATE": 85,
        "ZERO_RATE": 10,
        "MISSING_MAPPING": 5,
    }
    posting_counts = Counter(
        rows[0]["Expected Invoice Posting Status"]
        for rows in invoice_rows.values()
    )
    assert posting_counts == {
        "CORRECT": 80,
        "INCORRECT_AMOUNT": 3,
        "MISSING_POSTING": 2,
        "NO_POSTING_ZERO_RATE": 10,
        "NO_POSTING_UNRESOLVED": 5,
    }

    material_counts = Counter(
        row["Material Reference Case"]
        for row in scenarios
        if row["Line Type"] == "ELIGIBLE_EXPENSE"
    )
    assert material_counts == {
        "EXACT_MATCH": 152,
        "NO_PURCHASING_REFERENCE": 5,
        "UNMATCHED_PURCHASING_REFERENCE": 3,
    }
    material_keys = {
        (row["Purchasing Document"], row["Item"]) for row in material_rows
    }
    assert len(material_keys) == 152
    for rows in invoice_rows.values():
        expenses = [
            row for row in rows if row["Line Type"] == "ELIGIBLE_EXPENSE"
        ]
        if expenses[0]["Expected WHT Treatment"] == "MISSING_MAPPING":
            assert all(
                row["Expected Invoice Calculated WHT"] == ""
                and row["Expected Reconciliation Difference"] == ""
                and row["Expected Reconciliation Status"] == "PENDING"
                for row in expenses
            )
        else:
            calculated = sum(
                (Decimal(row["Expected Line WHT Amount"]) for row in expenses),
                Decimal("0.00"),
            )
            assert calculated.quantize(Decimal("0.01")) == Decimal(
                expenses[0]["Expected Invoice Calculated WHT"]
            )

    assert all(not row["Cost Type"] for row in open_rows)
    assert sum(row["GL Account"] == "210000" for row in open_rows) == 100
    assert sum(row["GL Account"] == "229220" for row in open_rows) == 83
    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["random_seed"] == 20261116
    assert metadata["invoice_count"] == 100
    assert metadata["manifest_key"] == list(MANIFEST_KEY_FIELDS)
    assert metadata["row_counts"]["eligible_expense_lines"] == 160
    assert metadata["actual_percentages"]["material_reference_eligible_lines"] == {
        "EXACT_MATCH": "95.000",
        "NO_PURCHASING_REFERENCE": "3.125",
        "UNMATCHED_PURCHASING_REFERENCE": "1.875",
    }
    assert "uat_scenarios.csv" in (
        output_dir / "README.md"
    ).read_text(encoding="utf-8")


@pytest.mark.django_db
def test_generator_rejects_incomplete_approved_mapping_state(
    tmp_path: Path,
) -> None:
    create_generator_reference_data(mapping_count=46)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(CommandError, match="exactly 47 approved mappings"):
        call_command("generate_uat_dataset", output_dir=str(output_dir))

    assert not output_dir.exists()
    assert WHTMapping.objects.count() == 46


@pytest.mark.parametrize("entity", [None, "", "WRONG"])
def test_manifest_validation_rejects_missing_or_incorrect_entity(entity) -> None:
    source = [{"Entity": "SYN01", "Document Number": "01", "Line Number": "001"}]
    manifest = [dict(source[0])]
    if entity is None:
        del manifest[0]["Entity"]
    else:
        manifest[0]["Entity"] = entity

    with pytest.raises(CommandError, match="business key"):
        Command()._validate_manifest_keys(source, manifest)


def test_manifest_validation_uses_all_three_key_fields() -> None:
    source = [
        {"Entity": "SYN01", "Document Number": "01", "Line Number": "001"},
        {"Entity": "SYN02", "Document Number": "01", "Line Number": "001"},
    ]
    command = Command()
    command._validate_manifest_keys(source, list(reversed(source)))

    with pytest.raises(CommandError, match="duplicate business keys"):
        command._validate_manifest_keys(source, [source[0], source[0]])


@pytest.mark.django_db
def test_generator_rejects_invalid_manifest_before_writing(
    tmp_path: Path, monkeypatch
) -> None:
    create_generator_reference_data()
    build = Command._build_dataset

    def build_with_wrong_entity(command, references):
        dataset = build(command, references)
        dataset.scenario_rows[0]["Entity"] = "WRONG"
        return dataset

    monkeypatch.setattr(Command, "_build_dataset", build_with_wrong_entity)
    output_dir = tmp_path / "must-not-exist"
    with pytest.raises(CommandError, match="business keys do not match"):
        call_command("generate_uat_dataset", output_dir=str(output_dir))
    assert not output_dir.exists()
