import csv

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from reference_data.models import CostType, SupplierCountry


@pytest.mark.django_db
def test_imports_reference_csv_files() -> None:
    call_command("import_reference_data")

    assert SupplierCountry.objects.count() == 2323
    assert CostType.objects.count() == 13
    assert SupplierCountry.objects.get(supplier_id="VEN100185").country_code == "GB"
    assert (
        SupplierCountry.objects.get(supplier_id="VEN101028").supplier_name
        == "Synthetic Supplier 0029"
    )
    assert CostType.objects.filter(code="RENTAL", active=True).exists()
    assert CostType.objects.filter(code="INTEREST", active=True).exists()
    assert CostType.objects.filter(code="ROYALTY", active=True).exists()


@pytest.mark.django_db
def test_reimport_is_idempotent() -> None:
    call_command("import_reference_data")
    call_command("import_reference_data")

    assert SupplierCountry.objects.count() == 2323
    assert CostType.objects.count() == 13


@pytest.mark.django_db
def test_duplicate_supplier_input_is_reported(tmp_path) -> None:
    countries = tmp_path / "countries.csv"
    with countries.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["Supplier", "Country", "Supplier Name"])
        writer.writeheader()
        writer.writerow(
            {"Supplier": "VEN001", "Country": "GB", "Supplier Name": "Supplier 1"}
        )
        writer.writerow(
            {"Supplier": "VEN001", "Country": "US", "Supplier Name": "Supplier 1"}
        )

    with pytest.raises(CommandError, match="Duplicate Supplier"):
        call_command(
            "import_reference_data",
            countries=str(countries),
        )
