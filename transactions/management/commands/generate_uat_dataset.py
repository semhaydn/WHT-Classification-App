import csv
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from reference_data.models import CostType, SupplierCountry, WHTMapping
from transactions.models import (
    GLEligibility,
    ImportBatch,
    MaterialData,
    MaterialMatch,
    OpenItem,
)
from transactions.services.gl_eligibility import (
    get_gl_eligibility_map,
    is_open_item_classifiable,
)
from transactions.services.reconciliation import reconcile_amounts, round_money


SEED = 20261116
EXPECTED_WHT_FROM = date(2024, 3, 1)
EXPECTED_WHT_TO = date(2027, 12, 1)
EXPECTED_MAPPING_COUNT = 47
EXPECTED_SUPPLIER_COUNT = 2323
EXPECTED_COST_TYPE_COUNT = 13
INVOICE_COUNT = 100
ELIGIBLE_LINE_COUNT = 160
ZERO = Decimal("0.00")
ONE = Decimal("1.00")

OPEN_ITEM_HEADERS = (
    "Title",
    "Entity",
    "Document Number",
    "Line Number",
    "Document Type",
    "Document Date",
    "Posting Date",
    "Document Reference",
    "Vendor Number",
    "Vendor Name",
    "Purchasing Document",
    "Purchasing Document Item",
    "MaterialData Key",
    "Material Group",
    "Material",
    "Material Description",
    "Document Currency Amount",
    "Document Currency",
    "Company Code Currency Amount",
    "Company Code Currency",
    "Group Currency Amount",
    "Group Currency",
    "GL Account",
    "GL Account Description",
    "Offsetting Account",
    "Text",
    "Cost Description",
    "Cost Type",
    "WHT Rate",
    "WHT Amount",
    "WHT Posted",
)

MATERIAL_DATA_HEADERS = (
    "MaterialData Key",
    "Purchasing Document",
    "Item",
    "Material",
    "Plant",
    "Order Unit",
    "Document Date",
    "Supplier/Supplying Plant",
    "Short Text",
    "Purch. Organization",
    "Net Price",
    "Currency",
    "Net Order Value",
    "Purchasing Info Rec.",
    "Purchasing Doc. Type",
    "Purch. Doc. Category",
    "Purchasing Group",
    "Material Group",
)

SCENARIO_HEADERS = (
    "Scenario ID",
    "Invoice Number",
    "Entity",
    "Document Number",
    "Line Number",
    "Line Type",
    "Supplier ID",
    "Supplier Name",
    "Supplier Country",
    "Expected Final Cost Type",
    "Expected Classification Eligibility",
    "Purchasing Document",
    "Purchasing Document Item",
    "Expected MaterialData Match Status",
    "Material Reference Case",
    "Expected WHT Status",
    "Expected WHT Treatment",
    "Expected Mapping ID",
    "Expected Mapping Version",
    "Expected WHT Rate",
    "Expected Line WHT Amount",
    "Expected Invoice Calculated WHT",
    "Expected Invoice Posted WHT Amount",
    "Expected Invoice Posting Status",
    "Expected Reconciliation Difference",
    "Expected Reconciliation Status",
    "Document Currency",
    "Document Currency Amount",
    "Posting Date",
    "Mapping Effective From",
    "Mapping Effective To",
    "Posting Date Rule Scenario",
    "Deliberate Discrepancy",
    "Scenario Description",
)

OUTPUT_FILES = (
    "open_line_items_uat.csv",
    "material_data_uat.csv",
    "uat_scenarios.csv",
    "metadata.json",
    "README.md",
)

POSITIVE_RATE_COUNT = 85
ZERO_RATE_COUNT = 10
MISSING_MAPPING_COUNT = 5
MANIFEST_KEY_FIELDS = ("Entity", "Document Number", "Line Number")

COMPANY_CURRENCIES = {
    "SYN01": "GBP",
    "SYN02": "EUR",
    "SYN03": "USD",
}

# Illustrative fixed USD factors; these are synthetic and are not tax inputs.
USD_PER_CURRENCY_UNIT = {
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "USD": Decimal("1.00"),
}

EXPENSE_GL_ACCOUNTS = ("210020", "570010")
CONTROL_GL_ACCOUNT = "210000"
WHT_GL_ACCOUNT = "229220"

COST_DESCRIPTIONS = {
    "INTEREST": "Synthetic interest expense on financing services",
    "LABOR": "Contract labor and technical support services",
    "RENTAL": "Equipment and site rental services",
    "REPAIR": "Scheduled repair and maintenance services",
    "ROYALTY": "Synthetic royalty and licensed-use fee",
}


@dataclass(frozen=True)
class MissingMappingCombination:
    supplier: SupplierCountry
    cost_type: CostType


@dataclass
class ReferenceState:
    suppliers: dict[str, SupplierCountry]
    cost_types: dict[int, CostType]
    mappings_by_key: dict[tuple[str, str, int], WHTMapping]
    positive_mappings: list[WHTMapping]
    zero_mappings: list[WHTMapping]
    missing_combinations: list[MissingMappingCombination]
    gl_eligibility: dict[str, bool]
    reference_counts: dict[str, int]
    multi_version_key_count: int


@dataclass
class ExpenseLine:
    line_number: str
    amount: Decimal
    gl_account: str
    material_case: str
    purchasing_document: str
    purchasing_item: str
    material_data_key: str
    wht_amount: Decimal | None
    description: str


@dataclass
class InvoicePlan:
    index: int
    document_number: str
    document_reference: str
    entity_code: str
    supplier: SupplierCountry
    cost_type: CostType
    mapping: WHTMapping | None
    wht_treatment: str
    posting_status: str
    complexity: int
    credit_note: bool
    document_currency: str
    company_currency: str
    document_date: date
    posting_date: date
    posting_date_rule_scenario: str
    expenses: list[ExpenseLine] = field(default_factory=list)
    calculated_wht: Decimal | None = None
    posted_wht: Decimal = ZERO
    reconciliation_difference: Decimal | None = None
    reconciliation_status: str = "PENDING"


@dataclass
class GeneratedDataset:
    invoices: list[InvoicePlan]
    open_item_rows: list[dict[str, str]]
    material_data_rows: list[dict[str, str]]
    scenario_rows: list[dict[str, str]]
    material_counts: Counter[str]
    zero_amount_line_count: int


class Command(BaseCommand):
    help = (
        "Generate a reproducible UAT CSV dataset from current reference data "
        "without writing database records."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--output-dir",
            default="data/uat_scenarios/realistic_v1",
            help="Output directory; existing output files are never overwritten.",
        )

    def handle(self, *args, **options) -> None:
        references = self._load_reference_state()
        output_dir = Path(options["output_dir"]).resolve()
        existing_outputs = [
            output_dir / filename
            for filename in OUTPUT_FILES
            if (output_dir / filename).exists()
        ]
        if existing_outputs:
            names = ", ".join(str(path) for path in existing_outputs)
            raise CommandError(
                f"Refusing to overwrite existing UAT output file(s): {names}"
            )

        dataset = self._build_dataset(references)
        self._validate_dataset(dataset, references)
        metadata = self._build_metadata(dataset, references)
        readme = self._build_readme(dataset)

        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_csv(
            output_dir / "open_line_items_uat.csv",
            OPEN_ITEM_HEADERS,
            dataset.open_item_rows,
        )
        self._write_csv(
            output_dir / "material_data_uat.csv",
            MATERIAL_DATA_HEADERS,
            dataset.material_data_rows,
        )
        self._write_csv(
            output_dir / "uat_scenarios.csv",
            SCENARIO_HEADERS,
            dataset.scenario_rows,
        )
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / "README.md").write_text(readme, encoding="utf-8")
        self._validate_written_files(output_dir, dataset)

        self.stdout.write(self.style.SUCCESS(f"Generated UAT dataset: {output_dir}"))
        self.stdout.write(
            "Rows: "
            f"open_items={len(dataset.open_item_rows)} "
            f"material_data={len(dataset.material_data_rows)} "
            f"scenario_manifest={len(dataset.scenario_rows)}"
        )
        self.stdout.write(
            "Invoices: "
            f"total={len(dataset.invoices)} "
            f"positive={POSITIVE_RATE_COUNT} zero_rate={ZERO_RATE_COUNT} "
            f"missing_mapping={MISSING_MAPPING_COUNT}"
        )

    def _load_reference_state(self) -> ReferenceState:
        supplier_rows = list(SupplierCountry.objects.order_by("supplier_id"))
        cost_type_rows = list(CostType.objects.order_by("pk"))
        all_mappings = list(
            WHTMapping.objects.select_related("cost_category").order_by("pk")
        )
        approved = [
            rule
            for rule in all_mappings
            if rule.status == WHTMapping.Status.APPROVED
        ]
        draft_count = sum(
            rule.status == WHTMapping.Status.DRAFT for rule in all_mappings
        )
        if (
            len(all_mappings) != EXPECTED_MAPPING_COUNT
            or len(approved) != EXPECTED_MAPPING_COUNT
            or draft_count != 0
        ):
            raise CommandError(
                "WHT reference state must contain exactly 47 approved mappings "
                f"and no drafts; found total={len(all_mappings)}, "
                f"approved={len(approved)}, draft={draft_count}."
            )
        if len(supplier_rows) != EXPECTED_SUPPLIER_COUNT:
            raise CommandError(
                "Expected exactly 2323 SupplierCountry records; "
                f"found {len(supplier_rows)}."
            )
        if len(cost_type_rows) != EXPECTED_COST_TYPE_COUNT:
            raise CommandError(
                f"Expected exactly {EXPECTED_COST_TYPE_COUNT} CostType records; "
                f"found {len(cost_type_rows)}."
            )
        transaction_counts = {
            "OpenItem": OpenItem.objects.count(),
            "MaterialData": MaterialData.objects.count(),
            "MaterialMatch": MaterialMatch.objects.count(),
            "ImportBatch": ImportBatch.objects.count(),
        }
        if any(transaction_counts.values()):
            raise CommandError(
                "UAT generation requires an empty transaction/import database; "
                f"found {transaction_counts}."
            )

        suppliers = {supplier.supplier_id: supplier for supplier in supplier_rows}
        cost_types = {cost_type.pk: cost_type for cost_type in cost_type_rows}
        grouped_mappings: dict[
            tuple[str, str, int], list[WHTMapping]
        ] = defaultdict(list)
        for rule in approved:
            if (
                rule.effective_from != EXPECTED_WHT_FROM
                or rule.effective_to != EXPECTED_WHT_TO
            ):
                raise CommandError(
                    f"WHT mapping ID {rule.pk} does not have the expected "
                    "effective dates."
                )
            if rule.approved_by_id is None or rule.approved_at is None:
                raise CommandError(
                    f"WHT mapping ID {rule.pk} is missing approval metadata."
                )
            supplier = suppliers.get(rule.supplier)
            if supplier is None or supplier.country_code != rule.country_code:
                raise CommandError(
                    f"WHT mapping ID {rule.pk} has no consistent SupplierCountry "
                    f"record for {rule.supplier}/{rule.country_code}."
                )
            if not rule.cost_category.active:
                raise CommandError(
                    f"WHT mapping ID {rule.pk} uses inactive Cost Type "
                    f"{rule.cost_category.code}."
                )
            grouped_mappings[
                (rule.supplier, rule.country_code, rule.cost_category_id)
            ].append(rule)

        mappings_by_key: dict[tuple[str, str, int], WHTMapping] = {}
        multi_version_key_count = 0
        for key, candidates in grouped_mappings.items():
            highest_version = max(rule.version for rule in candidates)
            latest = [
                rule for rule in candidates if rule.version == highest_version
            ]
            if len(latest) != 1:
                raise CommandError(
                    "The latest approved WHT mapping is ambiguous for "
                    f"{key[0]}/{key[1]}/{cost_types[key[2]].code}."
                )
            mappings_by_key[key] = latest[0]
            if len(candidates) > 1:
                multi_version_key_count += 1

        positive_mappings = sorted(
            (
                rule
                for rule in mappings_by_key.values()
                if rule.wht_rate > 0
            ),
            key=lambda rule: rule.pk,
        )
        zero_mappings = sorted(
            (
                rule
                for rule in mappings_by_key.values()
                if rule.wht_rate == 0
            ),
            key=lambda rule: rule.pk,
        )
        if not positive_mappings:
            raise CommandError("No approved positive-rate mapping is available.")
        if not zero_mappings:
            raise CommandError(
                "No approved zero-rate mapping is available for explicit No WHT."
            )

        active_cost_types = [
            cost_type for cost_type in cost_type_rows if cost_type.active
        ]
        missing_combinations = [
            MissingMappingCombination(supplier, cost_type)
            for supplier in supplier_rows
            for cost_type in active_cost_types
            if (
                supplier.supplier_id,
                supplier.country_code,
                cost_type.pk,
            )
            not in mappings_by_key
        ]
        if len(missing_combinations) < MISSING_MAPPING_COUNT:
            raise CommandError(
                "Fewer than five active supplier-country-Cost Type combinations "
                "without an approved mapping are available."
            )

        gl_accounts = (
            CONTROL_GL_ACCOUNT,
            WHT_GL_ACCOUNT,
            *EXPENSE_GL_ACCOUNTS,
        )
        eligibility = get_gl_eligibility_map(gl_accounts)
        for account, label in (
            (CONTROL_GL_ACCOUNT, "control"),
            (WHT_GL_ACCOUNT, "WHT posting"),
        ):
            sample = OpenItem(
                gl_account=account,
                document_currency_amount=Decimal("100.00"),
            )
            if is_open_item_classifiable(sample, eligibility, set()):
                raise CommandError(
                    f"The {label} G/L account {account} is classifiable under "
                    "current GLEligibility settings."
                )
        for account in EXPENSE_GL_ACCOUNTS:
            sample = OpenItem(
                gl_account=account,
                document_currency_amount=Decimal("100.00"),
            )
            if not is_open_item_classifiable(sample, eligibility, set()):
                raise CommandError(
                    f"Expense G/L account {account} is not eligible under "
                    "current GLEligibility settings."
                )

        return ReferenceState(
            suppliers=suppliers,
            cost_types=cost_types,
            mappings_by_key=mappings_by_key,
            positive_mappings=positive_mappings,
            zero_mappings=zero_mappings,
            missing_combinations=missing_combinations,
            gl_eligibility=eligibility,
            reference_counts={
                "WHTMapping": len(all_mappings),
                "WHTMapping_approved": len(approved),
                "WHTMapping_draft": draft_count,
                "SupplierCountry": len(supplier_rows),
                "CostType": len(cost_type_rows),
                "CostType_active": len(active_cost_types),
                "GLEligibility": GLEligibility.objects.count(),
                **transaction_counts,
            },
            multi_version_key_count=multi_version_key_count,
        )

    def _build_dataset(self, references: ReferenceState) -> GeneratedDataset:
        rng = random.Random(SEED)
        complexities = [1] * 60 + [2] * 30 + [4] * 10
        treatments = (
            ["POSITIVE_RATE"] * POSITIVE_RATE_COUNT
            + ["ZERO_RATE"] * ZERO_RATE_COUNT
            + ["MISSING_MAPPING"] * MISSING_MAPPING_COUNT
        )
        rng.shuffle(complexities)
        rng.shuffle(treatments)

        positive_postings = (
            ["CORRECT"] * 80
            + ["INCORRECT_AMOUNT"] * 3
            + ["MISSING_POSTING"] * 2
        )
        rng.shuffle(positive_postings)
        missing_combinations = rng.sample(
            references.missing_combinations,
            MISSING_MAPPING_COUNT,
        )
        credit_invoice_indexes = set(rng.sample(range(INVOICE_COUNT), 8))

        invoices: list[InvoicePlan] = []
        positive_index = 0
        missing_index = 0
        for index in range(INVOICE_COUNT):
            treatment = treatments[index]
            mapping: WHTMapping | None
            if treatment == "POSITIVE_RATE":
                mapping = rng.choice(references.positive_mappings)
                supplier = references.suppliers[mapping.supplier]
                cost_type = mapping.cost_category
                posting_status = positive_postings[positive_index]
                positive_index += 1
            elif treatment == "ZERO_RATE":
                mapping = references.zero_mappings[0]
                supplier = references.suppliers[mapping.supplier]
                cost_type = mapping.cost_category
                posting_status = "NO_POSTING_ZERO_RATE"
            else:
                combination = missing_combinations[missing_index]
                missing_index += 1
                supplier = combination.supplier
                cost_type = combination.cost_type
                mapping = None
                posting_status = "NO_POSTING_UNRESOLVED"

            entity = ("SYN01", "SYN02", "SYN03")[index % 3]
            document_currency = ("GBP", "USD", "EUR")[index % 3]
            document_date = date(2024, 1, 1) + timedelta(
                days=rng.randint(0, 900)
            )
            posting_date = document_date + timedelta(days=rng.randint(0, 45))
            rule_scenario = ""
            if treatment == "POSITIVE_RATE" and positive_index <= 4:
                edge_index = positive_index - 1
                if edge_index < 2:
                    document_date = date(2023, 10, 1) + timedelta(
                        days=edge_index * 30
                    )
                    posting_date = date(2023, 12, 15) + timedelta(
                        days=edge_index * 30
                    )
                    rule_scenario = "POSTING_BEFORE_EFFECTIVE_FROM"
                else:
                    document_date = date(2028, 1, 1) + timedelta(
                        days=(edge_index - 2) * 30
                    )
                    posting_date = date(2028, 1, 15) + timedelta(
                        days=(edge_index - 2) * 30
                    )
                    rule_scenario = "POSTING_AFTER_EFFECTIVE_TO"

            invoices.append(
                InvoicePlan(
                    index=index,
                    document_number=f"0000{index + 1:08d}",
                    document_reference=f"UAT-REF-{index + 1:06d}",
                    entity_code=entity,
                    supplier=supplier,
                    cost_type=cost_type,
                    mapping=mapping,
                    wht_treatment=treatment,
                    posting_status=posting_status,
                    complexity=complexities[index],
                    credit_note=index in credit_invoice_indexes,
                    document_currency=document_currency,
                    company_currency=COMPANY_CURRENCIES[entity],
                    document_date=document_date,
                    posting_date=posting_date,
                    posting_date_rule_scenario=rule_scenario,
                )
            )

        material_cases = (
            ["EXACT_MATCH"] * 152
            + ["NO_PURCHASING_REFERENCE"] * 5
            + ["UNMATCHED_PURCHASING_REFERENCE"] * 3
        )
        rng.shuffle(material_cases)

        open_rows: list[dict[str, str]] = []
        material_rows: list[dict[str, str]] = []
        scenario_rows: list[dict[str, str]] = []
        material_counts: Counter[str] = Counter()
        purchase_counter = 0
        zero_amount_line_count = 0

        for invoice in invoices:
            invoice_expenses: list[ExpenseLine] = []
            for line_index in range(invoice.complexity):
                line_number = f"{line_index + 1:03d}"
                material_case = material_cases.pop()
                if material_case == "NO_PURCHASING_REFERENCE":
                    purchase_document = ""
                    purchasing_item = ""
                    material_data_key = ""
                else:
                    purchase_counter += 1
                    purchase_document = f"4500{purchase_counter:08d}"
                    purchasing_item = f"{purchase_counter * 10:05d}"
                    material_data_key = (
                        f"{purchase_document}|{purchasing_item}"
                    )

                magnitude = Decimal(rng.randint(25_000, 2_500_000)) / 100
                amount = -magnitude if invoice.credit_note else magnitude
                account = EXPENSE_GL_ACCOUNTS[
                    (invoice.index + line_index) % len(EXPENSE_GL_ACCOUNTS)
                ]
                description = self._cost_description(invoice.cost_type)
                line_wht = self._line_wht_amount(invoice, amount)
                expense = ExpenseLine(
                    line_number=line_number,
                    amount=amount,
                    gl_account=account,
                    material_case=material_case,
                    purchasing_document=purchase_document,
                    purchasing_item=purchasing_item,
                    material_data_key=material_data_key,
                    wht_amount=line_wht,
                    description=description,
                )
                invoice_expenses.append(expense)
                material_counts[material_case] += 1

                if material_case == "EXACT_MATCH":
                    material_rows.append(
                        self._material_data_row(invoice, expense, rng)
                    )

            invoice.expenses = invoice_expenses
            if invoice.wht_treatment == "MISSING_MAPPING":
                invoice.calculated_wht = None
            else:
                invoice.calculated_wht = round_money(
                    sum(
                        (
                            line.wht_amount
                            for line in invoice.expenses
                            if line.wht_amount is not None
                        ),
                        ZERO,
                    )
                )
            invoice.posted_wht = self._posted_wht_amount(invoice)
            (
                invoice.reconciliation_difference,
                invoice.reconciliation_status,
            ) = reconcile_amounts(
                invoice.calculated_wht,
                invoice.posted_wht,
            )

            invoice_rows: list[
                tuple[dict[str, str], str, ExpenseLine | None]
            ] = []
            for expense in invoice.expenses:
                invoice_rows.append(
                    (
                        self._open_item_row(
                            invoice,
                            expense.line_number,
                            expense.amount,
                            expense.gl_account,
                            "Expense service posting",
                            expense.description,
                            purchasing_document=expense.purchasing_document,
                            purchasing_item=expense.purchasing_item,
                            material_data_key=expense.material_data_key,
                            material_data_exists=(
                                expense.material_case == "EXACT_MATCH"
                            ),
                        ),
                        "ELIGIBLE_EXPENSE",
                        expense,
                    )
                )

            if invoice.index < 10:
                zero_amount_line_count += 1
                invoice_rows.append(
                    (
                        self._open_item_row(
                            invoice,
                            "800",
                            ZERO,
                            EXPENSE_GL_ACCOUNTS[invoice.index % 2],
                            "Zero-value expense line",
                            "Zero-amount service line; ineligible for classification",
                        ),
                        "ZERO_AMOUNT_EXPENSE",
                        None,
                    )
                )

            control_amount = round_money(
                -sum((line.amount for line in invoice.expenses), ZERO)
            )
            invoice_rows.append(
                (
                    self._open_item_row(
                        invoice,
                        "900",
                        control_amount,
                        CONTROL_GL_ACCOUNT,
                        "Vendor invoice control posting",
                        "Synthetic vendor control posting",
                        offsetting_account=EXPENSE_GL_ACCOUNTS[0],
                    ),
                    "CONTROL",
                    None,
                )
            )
            if invoice.posting_status in {"CORRECT", "INCORRECT_AMOUNT"}:
                invoice_rows.append(
                    (
                        self._open_item_row(
                            invoice,
                            "901",
                            invoice.posted_wht,
                            WHT_GL_ACCOUNT,
                            "Withholding tax payable",
                            "Synthetic posted WHT control line",
                            offsetting_account=CONTROL_GL_ACCOUNT,
                        ),
                        "WHT_POSTING",
                        None,
                    )
                )

            for row, line_type, expense in invoice_rows:
                open_rows.append(row)
                scenario_rows.append(
                    self._scenario_row(
                        invoice,
                        row,
                        line_type,
                        expense,
                        len(scenario_rows) + 1,
                    )
                )

        return GeneratedDataset(
            invoices=invoices,
            open_item_rows=open_rows,
            material_data_rows=material_rows,
            scenario_rows=scenario_rows,
            material_counts=material_counts,
            zero_amount_line_count=zero_amount_line_count,
        )

    def _line_wht_amount(
        self,
        invoice: InvoicePlan,
        amount: Decimal,
    ) -> Decimal | None:
        if invoice.wht_treatment == "MISSING_MAPPING":
            return None
        if invoice.mapping is None:
            raise CommandError("Mapped scenario has no approved WHT mapping.")
        return round_money(amount * invoice.mapping.wht_rate)

    def _posted_wht_amount(self, invoice: InvoicePlan) -> Decimal:
        if invoice.wht_treatment == "MISSING_MAPPING":
            return ZERO
        if invoice.wht_treatment == "ZERO_RATE":
            return ZERO
        if invoice.calculated_wht is None:
            raise CommandError("Positive-rate scenario has unresolved WHT.")
        if invoice.posting_status == "CORRECT":
            return -invoice.calculated_wht
        if invoice.posting_status == "INCORRECT_AMOUNT":
            return round_money(-invoice.calculated_wht + ONE)
        if invoice.posting_status == "MISSING_POSTING":
            return ZERO
        raise CommandError(
            f"Unsupported positive-rate posting status: {invoice.posting_status}."
        )

    def _cost_description(self, cost_type: CostType) -> str:
        return COST_DESCRIPTIONS.get(
            cost_type.code,
            f"Synthetic {cost_type.name.lower()} service or fee",
        )

    def _material_data_row(
        self,
        invoice: InvoicePlan,
        expense: ExpenseLine,
        rng: random.Random,
    ) -> dict[str, str]:
        if not expense.purchasing_document or not expense.purchasing_item:
            raise CommandError("A matched expense line has no purchasing reference.")
        price = Decimal(rng.randint(10_000, 300_000)) / 100
        quantity = rng.randint(1, 25)
        net_order_value = round_money(price * quantity)
        material = f"SVC-{invoice.cost_type.code.replace(' ', '-')}"
        short_text = expense.description
        return {
            "MaterialData Key": expense.material_data_key,
            "Purchasing Document": expense.purchasing_document,
            "Item": expense.purchasing_item,
            "Material": material,
            "Plant": f"P{invoice.index % 3 + 1:03d}",
            "Order Unit": "EA",
            "Document Date": invoice.document_date.isoformat(),
            "Supplier/Supplying Plant": (
                f"{invoice.supplier.supplier_id} "
                f"{invoice.supplier.supplier_name}"
            ),
            "Short Text": short_text,
            "Purch. Organization": "1000",
            "Net Price": f"{price:.2f}",
            "Currency": invoice.document_currency,
            "Net Order Value": f"{net_order_value:.2f}",
            "Purchasing Info Rec.": f"INFO{invoice.index + 1:06d}",
            "Purchasing Doc. Type": "NB",
            "Purch. Doc. Category": "F",
            "Purchasing Group": "001",
            "Material Group": "SERVICES",
        }

    def _open_item_row(
        self,
        invoice: InvoicePlan,
        line_number: str,
        amount: Decimal,
        gl_account: str,
        gl_description: str,
        text: str,
        *,
        purchasing_document: str = "",
        purchasing_item: str = "",
        material_data_key: str = "",
        material_data_exists: bool = False,
        offsetting_account: str = CONTROL_GL_ACCOUNT,
    ) -> dict[str, str]:
        company_amount = self._convert_currency(
            amount,
            invoice.document_currency,
            invoice.company_currency,
        )
        group_amount = self._convert_currency(
            amount,
            invoice.document_currency,
            "USD",
        )
        has_material = material_data_exists
        return {
            "Title": f"UAT {invoice.document_number} line {line_number}",
            "Entity": invoice.entity_code,
            "Document Number": invoice.document_number,
            "Line Number": line_number,
            "Document Type": "KG" if invoice.credit_note else "KR",
            "Document Date": invoice.document_date.isoformat(),
            "Posting Date": invoice.posting_date.isoformat(),
            "Document Reference": invoice.document_reference,
            "Vendor Number": invoice.supplier.supplier_id,
            "Vendor Name": invoice.supplier.supplier_name,
            "Purchasing Document": purchasing_document,
            "Purchasing Document Item": purchasing_item,
            "MaterialData Key": material_data_key,
            "Material Group": "SERVICES" if has_material else "",
            "Material": (
                f"SVC-{invoice.cost_type.code.replace(' ', '-')}"
                if has_material
                else ""
            ),
            "Material Description": (
                self._cost_description(invoice.cost_type) if has_material else ""
            ),
            "Document Currency Amount": f"{amount:.2f}",
            "Document Currency": invoice.document_currency,
            "Company Code Currency Amount": f"{company_amount:.2f}",
            "Company Code Currency": invoice.company_currency,
            "Group Currency Amount": f"{group_amount:.2f}",
            "Group Currency": "USD",
            "GL Account": gl_account,
            "GL Account Description": gl_description,
            "Offsetting Account": offsetting_account,
            "Text": text,
            "Cost Description": "",
            "Cost Type": "",
            "WHT Rate": "",
            "WHT Amount": "",
            "WHT Posted": "",
        }

    def _convert_currency(
        self,
        amount: Decimal,
        source_currency: str,
        target_currency: str,
    ) -> Decimal:
        source_rate = USD_PER_CURRENCY_UNIT[source_currency]
        target_rate = USD_PER_CURRENCY_UNIT[target_currency]
        return round_money(amount * source_rate / target_rate)

    def _scenario_row(
        self,
        invoice: InvoicePlan,
        source_row: dict[str, str],
        line_type: str,
        expense: ExpenseLine | None,
        scenario_number: int,
    ) -> dict[str, str]:
        if line_type == "ELIGIBLE_EXPENSE":
            eligibility = "ELIGIBLE"
            expected_wht_status = {
                "POSITIVE_RATE": "MATCHED",
                "ZERO_RATE": "NO_WHT",
                "MISSING_MAPPING": "REQUIRES_REVIEW",
            }[invoice.wht_treatment]
            expected_cost_type = invoice.cost_type.code
            expected_line_wht = (
                ""
                if expense is None or expense.wht_amount is None
                else f"{expense.wht_amount:.2f}"
            )
            material_case = expense.material_case if expense else ""
            expected_material = (
                "MATCHED" if material_case == "EXACT_MATCH" else "NOT_FOUND"
            )
            mapping = invoice.mapping
        elif line_type == "ZERO_AMOUNT_EXPENSE":
            eligibility = "INELIGIBLE_ZERO_AMOUNT"
            expected_wht_status = "NOT_DETERMINED"
            expected_cost_type = ""
            expected_line_wht = ""
            material_case = "NO_PURCHASING_REFERENCE"
            expected_material = "NOT_FOUND"
            mapping = None
        elif line_type == "CONTROL":
            eligibility = "INELIGIBLE_CONTROL_GL"
            expected_wht_status = "NOT_APPLICABLE"
            expected_cost_type = ""
            expected_line_wht = ""
            material_case = "NO_PURCHASING_REFERENCE"
            expected_material = "NOT_FOUND"
            mapping = None
        else:
            eligibility = "INELIGIBLE_WHT_GL"
            expected_wht_status = "NOT_APPLICABLE"
            expected_cost_type = ""
            expected_line_wht = ""
            material_case = "NO_PURCHASING_REFERENCE"
            expected_material = "NOT_FOUND"
            mapping = None

        description = self._invoice_description(invoice)
        if line_type == "ELIGIBLE_EXPENSE" and expense:
            description += f" {self._material_case_description(expense.material_case)}"
        elif line_type != "ELIGIBLE_EXPENSE":
            description += f" {line_type.lower().replace('_', ' ')}."

        deliberate_discrepancy = invoice.posting_status in {
            "INCORRECT_AMOUNT",
            "MISSING_POSTING",
        }
        return {
            "Scenario ID": f"UAT-SCN-{scenario_number:06d}",
            "Invoice Number": invoice.document_number,
            "Entity": source_row["Entity"],
            "Document Number": source_row["Document Number"],
            "Line Number": source_row["Line Number"],
            "Line Type": line_type,
            "Supplier ID": invoice.supplier.supplier_id,
            "Supplier Name": invoice.supplier.supplier_name,
            "Supplier Country": invoice.supplier.country_code,
            "Expected Final Cost Type": expected_cost_type,
            "Expected Classification Eligibility": eligibility,
            "Purchasing Document": source_row["Purchasing Document"],
            "Purchasing Document Item": source_row["Purchasing Document Item"],
            "Expected MaterialData Match Status": expected_material,
            "Material Reference Case": material_case,
            "Expected WHT Status": expected_wht_status,
            "Expected WHT Treatment": invoice.wht_treatment,
            "Expected Mapping ID": str(mapping.pk) if mapping else "",
            "Expected Mapping Version": str(mapping.version) if mapping else "",
            "Expected WHT Rate": (
                f"{mapping.wht_rate:.6f}" if mapping else ""
            ),
            "Expected Line WHT Amount": expected_line_wht,
            "Expected Invoice Calculated WHT": (
                ""
                if invoice.calculated_wht is None
                else f"{invoice.calculated_wht:.2f}"
            ),
            "Expected Invoice Posted WHT Amount": f"{invoice.posted_wht:.2f}",
            "Expected Invoice Posting Status": invoice.posting_status,
            "Expected Reconciliation Difference": (
                ""
                if invoice.reconciliation_difference is None
                else f"{invoice.reconciliation_difference:.2f}"
            ),
            "Expected Reconciliation Status": invoice.reconciliation_status,
            "Document Currency": invoice.document_currency,
            "Document Currency Amount": source_row["Document Currency Amount"],
            "Posting Date": invoice.posting_date.isoformat(),
            "Mapping Effective From": (
                mapping.effective_from.isoformat() if mapping else ""
            ),
            "Mapping Effective To": (
                mapping.effective_to.isoformat() if mapping else ""
            ),
            "Posting Date Rule Scenario": invoice.posting_date_rule_scenario,
            "Deliberate Discrepancy": (
                "YES" if deliberate_discrepancy else "NO"
            ),
            "Scenario Description": description.strip(),
        }

    def _invoice_description(self, invoice: InvoicePlan) -> str:
        if invoice.wht_treatment == "POSITIVE_RATE":
            text = (
                "Approved positive-rate mapping "
                f"{invoice.cost_type.code} at "
                f"{invoice.mapping.wht_rate:.6f}."
            )
        elif invoice.wht_treatment == "ZERO_RATE":
            text = (
                "Approved zero-rate mapping explicitly represents No WHT; "
                "this is not a missing mapping."
            )
        else:
            text = (
                f"No approved mapping exists for {invoice.supplier.supplier_id}/"
                f"{invoice.supplier.country_code}/{invoice.cost_type.code}; "
                "WHT remains unresolved, not zero."
            )
        if invoice.credit_note:
            text += " Synthetic credit note with signed negative expense postings."
        if invoice.posting_status == "INCORRECT_AMOUNT":
            text += (
                " Deliberately incorrect WHT posting is 1.00 above the "
                "opposite-sign calculated amount."
            )
        elif invoice.posting_status == "MISSING_POSTING":
            text += " Deliberately missing WHT posting creates a discrepancy."
        if invoice.posting_date_rule_scenario:
            text += (
                " Posting date is outside the mapping effective interval; "
                "the latest approved mapping is selected independently of date."
            )
        return text

    def _material_case_description(self, material_case: str) -> str:
        return {
            "EXACT_MATCH": "One exact MaterialData key is supplied.",
            "NO_PURCHASING_REFERENCE": (
                "Purchasing document and item references are blank."
            ),
            "UNMATCHED_PURCHASING_REFERENCE": (
                "Purchasing reference is present but no MaterialData row exists."
            ),
        }[material_case]

    def _validate_dataset(
        self,
        dataset: GeneratedDataset,
        references: ReferenceState,
    ) -> None:
        if len(dataset.invoices) != INVOICE_COUNT:
            raise CommandError("Generated invoice count is not exactly 100.")
        if len({invoice.document_number for invoice in dataset.invoices}) != 100:
            raise CommandError("Generated document numbers are not unique.")
        if len({invoice.document_reference for invoice in dataset.invoices}) != 100:
            raise CommandError("Generated document references are not unique.")

        complexity_counts = Counter(
            invoice.complexity for invoice in dataset.invoices
        )
        if complexity_counts != Counter({1: 60, 2: 30, 4: 10}):
            raise CommandError(
                f"Invoice complexity distribution mismatch: {complexity_counts}."
            )
        if sum(invoice.complexity for invoice in dataset.invoices) != ELIGIBLE_LINE_COUNT:
            raise CommandError("Generated eligible expense line count is not 160.")

        treatment_counts = Counter(
            invoice.wht_treatment for invoice in dataset.invoices
        )
        if treatment_counts != Counter(
            {
                "POSITIVE_RATE": 85,
                "ZERO_RATE": 10,
                "MISSING_MAPPING": 5,
            }
        ):
            raise CommandError(f"WHT treatment distribution mismatch: {treatment_counts}.")

        posting_counts = Counter(
            (
                "CORRECT_POSITIVE"
                if invoice.posting_status == "CORRECT"
                else "DELIBERATE_POSITIVE_DISCREPANCY"
                if invoice.posting_status
                in {"INCORRECT_AMOUNT", "MISSING_POSTING"}
                else "ZERO_RATE_NO_POSTING"
                if invoice.wht_treatment == "ZERO_RATE"
                else "MISSING_MAPPING_NO_POSTING"
            )
            for invoice in dataset.invoices
        )
        if posting_counts != Counter(
            {
                "CORRECT_POSITIVE": 80,
                "DELIBERATE_POSITIVE_DISCREPANCY": 5,
                "ZERO_RATE_NO_POSTING": 10,
                "MISSING_MAPPING_NO_POSTING": 5,
            }
        ):
            raise CommandError(f"Posting outcome distribution mismatch: {posting_counts}.")

        expected_material_counts = Counter(
            {
                "EXACT_MATCH": 152,
                "NO_PURCHASING_REFERENCE": 5,
                "UNMATCHED_PURCHASING_REFERENCE": 3,
            }
        )
        if dataset.material_counts != expected_material_counts:
            raise CommandError(
                f"Material reference distribution mismatch: {dataset.material_counts}."
            )
        if len(dataset.material_data_rows) != 152:
            raise CommandError("Exactly 152 MaterialData rows are required.")
        if dataset.zero_amount_line_count != 10:
            raise CommandError("Exactly 10 zero-amount ineligible lines are required.")

        business_keys = [
            (
                row["Entity"],
                row["Document Number"],
                row["Line Number"],
            )
            for row in dataset.open_item_rows
        ]
        if len(business_keys) != len(set(business_keys)):
            raise CommandError("Duplicate OpenItem business keys were generated.")
        material_keys = [
            (row["Purchasing Document"], row["Item"])
            for row in dataset.material_data_rows
        ]
        if len(material_keys) != len(set(material_keys)):
            raise CommandError("Duplicate MaterialData purchasing keys were generated.")

        material_key_set = set(material_keys)
        matched_line_keys: set[tuple[str, str]] = set()
        unmatched_line_keys: set[tuple[str, str]] = set()
        no_reference_count = 0
        eligible_rows = [
            row
            for row in dataset.open_item_rows
            if row["GL Account"] in EXPENSE_GL_ACCOUNTS
            and Decimal(row["Document Currency Amount"]) != 0
        ]
        if len(eligible_rows) != ELIGIBLE_LINE_COUNT:
            raise CommandError("The generated eligible line count is not 160.")
        for row in eligible_rows:
            purchase_key = (
                row["Purchasing Document"],
                row["Purchasing Document Item"],
            )
            if not all(purchase_key):
                no_reference_count += 1
            elif purchase_key in material_key_set:
                matched_line_keys.add(purchase_key)
            else:
                unmatched_line_keys.add(purchase_key)
        if (
            len(matched_line_keys),
            no_reference_count,
            len(unmatched_line_keys),
        ) != (152, 5, 3):
            raise CommandError(
                "Material match counts must be 152 matched, 5 without references, "
                "and 3 referenced but unmatched."
            )
        if matched_line_keys != material_key_set:
            raise CommandError(
                "MaterialData rows do not correspond exactly to matched expense lines."
            )
        if material_key_set.intersection(unmatched_line_keys):
            raise CommandError(
                "A deliberately unmatched purchasing reference has MaterialData."
            )

        for invoice in dataset.invoices:
            supplier = references.suppliers.get(invoice.supplier.supplier_id)
            if supplier is None or supplier.country_code != invoice.supplier.country_code:
                raise CommandError(
                    f"Supplier-country identity is inconsistent for "
                    f"{invoice.supplier.supplier_id}."
                )
            if not invoice.cost_type.active:
                raise CommandError(
                    f"Inactive Cost Type selected: {invoice.cost_type.code}."
                )
            if invoice.wht_treatment == "POSITIVE_RATE":
                if (
                    invoice.mapping is None
                    or invoice.mapping.status != WHTMapping.Status.APPROVED
                    or invoice.mapping.wht_rate <= 0
                ):
                    raise CommandError(
                        "A positive-rate invoice does not use an approved "
                        "positive-rate mapping."
                    )
                mapping_key = (
                    invoice.supplier.supplier_id,
                    invoice.supplier.country_code,
                    invoice.cost_type.pk,
                )
                if references.mappings_by_key.get(mapping_key) != invoice.mapping:
                    raise CommandError(
                        "A positive-rate invoice does not use its latest approved mapping."
                    )
            elif invoice.wht_treatment == "ZERO_RATE":
                if (
                    invoice.mapping is None
                    or invoice.mapping.status != WHTMapping.Status.APPROVED
                    or invoice.mapping.wht_rate != 0
                ):
                    raise CommandError(
                        "A No WHT invoice must use an approved zero-rate mapping."
                    )
            else:
                key = (
                    invoice.supplier.supplier_id,
                    invoice.supplier.country_code,
                    invoice.cost_type.pk,
                )
                if key in references.mappings_by_key:
                    raise CommandError(
                        "A missing-mapping scenario has an approved mapping."
                    )

            for expense in invoice.expenses:
                sample = OpenItem(
                    gl_account=expense.gl_account,
                    document_currency_amount=expense.amount,
                )
                if not is_open_item_classifiable(
                    sample,
                    references.gl_eligibility,
                    set(),
                ):
                    raise CommandError(
                        f"Expense line uses ineligible G/L {expense.gl_account}."
                    )
                expected_line_amount = self._line_wht_amount(
                    invoice,
                    expense.amount,
                )
                if expected_line_amount != expense.wht_amount:
                    raise CommandError(
                        "Expected WHT line amount does not reproduce application "
                        "Decimal rounding."
                    )

            calculated = (
                None
                if invoice.wht_treatment == "MISSING_MAPPING"
                else round_money(
                    sum(
                        (
                            line.wht_amount
                            for line in invoice.expenses
                            if line.wht_amount is not None
                        ),
                        ZERO,
                    )
                )
            )
            if calculated != invoice.calculated_wht:
                raise CommandError("Invoice WHT aggregation is inconsistent.")
            if invoice.wht_treatment == "MISSING_MAPPING":
                if (
                    invoice.calculated_wht is not None
                    or invoice.reconciliation_difference is not None
                    or invoice.reconciliation_status != "PENDING"
                ):
                    raise CommandError(
                        "Missing mappings must remain unresolved and pending."
                    )
            elif invoice.posting_status in {
                "CORRECT",
                "INCORRECT_AMOUNT",
                "MISSING_POSTING",
            }:
                difference, status = reconcile_amounts(
                    invoice.calculated_wht,
                    invoice.posted_wht,
                )
                if (
                    difference != invoice.reconciliation_difference
                    or status != invoice.reconciliation_status
                ):
                    raise CommandError(
                        "Invoice reconciliation does not match application logic."
                    )
                if (
                    invoice.posting_status in {"INCORRECT_AMOUNT", "MISSING_POSTING"}
                    and (
                        difference is None
                        or abs(difference) <= Decimal("0.01")
                    )
                ):
                    raise CommandError(
                        "A deliberate positive-rate discrepancy must exceed 0.01."
                    )
            else:
                difference, status = reconcile_amounts(
                    invoice.calculated_wht,
                    invoice.posted_wht,
                )
                if (
                    difference != invoice.reconciliation_difference
                    or status != invoice.reconciliation_status
                    or status != "RECONCILED"
                ):
                    raise CommandError(
                        "Zero-rate invoice must reconcile with no WHT posting."
                    )

        for row in dataset.open_item_rows:
            for field_name in (
                "Document Currency Amount",
                "Company Code Currency Amount",
                "Group Currency Amount",
            ):
                try:
                    Decimal(row[field_name])
                except (InvalidOperation, ValueError) as error:
                    raise CommandError(
                        f"Invalid decimal in generated field {field_name}."
                    ) from error
                if not Decimal(row[field_name]).is_finite():
                    raise CommandError(
                        f"Non-finite decimal in generated field {field_name}."
                    )
            for field_name in ("Document Date", "Posting Date"):
                date.fromisoformat(row[field_name])
            if row["Document Currency"] not in USD_PER_CURRENCY_UNIT:
                raise CommandError("Unsupported generated document currency.")
            expected_company_amount = self._convert_currency(
                Decimal(row["Document Currency Amount"]),
                row["Document Currency"],
                row["Company Code Currency"],
            )
            expected_group_amount = self._convert_currency(
                Decimal(row["Document Currency Amount"]),
                row["Document Currency"],
                row["Group Currency"],
            )
            if (
                expected_company_amount
                != Decimal(row["Company Code Currency Amount"])
                or expected_group_amount != Decimal(row["Group Currency Amount"])
            ):
                raise CommandError("Synthetic currency conversion is inconsistent.")
            if row["GL Account"] in {CONTROL_GL_ACCOUNT, WHT_GL_ACCOUNT}:
                sample = OpenItem(
                    gl_account=row["GL Account"],
                    document_currency_amount=Decimal(
                        row["Document Currency Amount"]
                    ),
                )
                if is_open_item_classifiable(
                    sample,
                    references.gl_eligibility,
                    set(),
                ):
                    raise CommandError(
                        f"Control/tax line G/L {row['GL Account']} is classifiable."
                    )

        for row in dataset.material_data_rows:
            for field_name in ("Net Price", "Net Order Value"):
                try:
                    value = Decimal(row[field_name])
                except (InvalidOperation, ValueError) as error:
                    raise CommandError(
                        f"Invalid MaterialData decimal in {field_name}."
                    ) from error
                if not value.is_finite():
                    raise CommandError(
                        f"Non-finite MaterialData amount in {field_name}."
                    )
            date.fromisoformat(row["Document Date"])
            if row["Currency"] not in USD_PER_CURRENCY_UNIT:
                raise CommandError("Unsupported generated MaterialData currency.")

        if sum(row["GL Account"] == CONTROL_GL_ACCOUNT for row in dataset.open_item_rows) != 100:
            raise CommandError("Exactly one control posting is required per invoice.")
        expected_tax_lines = sum(
            invoice.posting_status in {"CORRECT", "INCORRECT_AMOUNT"}
            for invoice in dataset.invoices
        )
        if sum(row["GL Account"] == WHT_GL_ACCOUNT for row in dataset.open_item_rows) != expected_tax_lines:
            raise CommandError("Generated WHT posting line count is inconsistent.")

        if len(dataset.scenario_rows) != len(dataset.open_item_rows):
            raise CommandError(
                "Scenario manifest must include every generated OpenItem line."
            )
        self._validate_manifest_keys(dataset.open_item_rows, dataset.scenario_rows)

    def _validate_manifest_keys(
        self,
        open_rows: list[dict[str, str]],
        scenario_rows: list[dict[str, str]],
    ) -> None:
        def keys(rows: list[dict[str, str]], label: str) -> list[tuple[str, ...]]:
            result = []
            for row in rows:
                values = tuple(row.get(field, "") for field in MANIFEST_KEY_FIELDS)
                if not all(value.strip() for value in values):
                    raise CommandError(f"{label} has a missing or blank business key.")
                result.append(values)
            if len(result) != len(set(result)):
                raise CommandError(f"{label} has duplicate business keys.")
            return result

        source_keys = keys(open_rows, "OpenItem")
        manifest_keys = keys(scenario_rows, "Scenario manifest")
        if set(source_keys) != set(manifest_keys):
            raise CommandError(
                "Scenario manifest business keys do not match OpenItem business keys."
            )

    def _build_metadata(
        self,
        dataset: GeneratedDataset,
        references: ReferenceState,
    ) -> dict[str, object]:
        wht_counts = Counter(
            invoice.wht_treatment for invoice in dataset.invoices
        )
        posting_counts = Counter(invoice.posting_status for invoice in dataset.invoices)
        total_expense_lines = sum(invoice.complexity for invoice in dataset.invoices)
        return {
            "generation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "random_seed": SEED,
            "reference_counts": references.reference_counts,
            "approved_mapping_counts": {
                "positive_rate": len(references.positive_mappings),
                "zero_rate": len(references.zero_mappings),
                "supplier_cost_type_keys_with_multiple_versions": (
                    references.multi_version_key_count
                ),
                "unique_mappings_used": len(
                    {
                        invoice.mapping.pk
                        for invoice in dataset.invoices
                        if invoice.mapping is not None
                    }
                ),
            },
            "invoice_count": len(dataset.invoices),
            "manifest_key": list(MANIFEST_KEY_FIELDS),
            "invoice_complexity_counts": {
                str(complexity): count
                for complexity, count in sorted(
                    Counter(
                        invoice.complexity for invoice in dataset.invoices
                    ).items()
                )
            },
            "wht_treatment_invoice_counts": dict(sorted(wht_counts.items())),
            "posting_outcome_invoice_counts": dict(sorted(posting_counts.items())),
            "material_reference_line_counts": dict(
                sorted(dataset.material_counts.items())
            ),
            "row_counts": {
                "open_line_items_uat.csv": len(dataset.open_item_rows),
                "material_data_uat.csv": len(dataset.material_data_rows),
                "uat_scenarios.csv": len(dataset.scenario_rows),
                "eligible_expense_lines": total_expense_lines,
                "zero_amount_ineligible_lines": dataset.zero_amount_line_count,
            },
            "actual_percentages": {
                "wht_treatment_invoices": {
                    key: self._percentage(count, len(dataset.invoices))
                    for key, count in sorted(wht_counts.items())
                },
                "posting_outcome_invoices": {
                    key: self._percentage(count, len(dataset.invoices))
                    for key, count in sorted(posting_counts.items())
                },
                "material_reference_eligible_lines": {
                    key: self._percentage(count, total_expense_lines)
                    for key, count in sorted(dataset.material_counts.items())
                },
            },
            "posting_date_independence_scenarios": sum(
                bool(invoice.posting_date_rule_scenario)
                for invoice in dataset.invoices
            ),
            "expense_gl_accounts": list(EXPENSE_GL_ACCOUNTS),
            "excluded_control_gl_accounts": [
                CONTROL_GL_ACCOUNT,
                WHT_GL_ACCOUNT,
            ],
            "synthetic_currency_assumptions": {
                "usd_per_currency_unit": {
                    key: str(value)
                    for key, value in USD_PER_CURRENCY_UNIT.items()
                },
                "group_currency": "USD",
                "company_currency_by_entity": COMPANY_CURRENCIES,
                "description": (
                    "Illustrative fixed factors used only to populate accounting "
                    "currency columns; WHT uses document-currency amounts only."
                ),
            },
        }

    def _percentage(self, count: int, total: int) -> str:
        return f"{Decimal(count) * 100 / Decimal(total):.3f}"

    def _build_readme(self, dataset: GeneratedDataset) -> str:
        return f"""# Realistic UAT dataset v1

All records are synthetic. These files are a reproducible test fixture, not tax
advice, production data, or approved accounting entries. The fixed generator
seed is `{SEED}`; generated transaction rows are stable for the same database
reference records.

## Files and upload

- `open_line_items_uat.csv`: accounting source lines. Upload through the
  existing Import Center using the Open Items import flow.
- `material_data_uat.csv`: 152 purchasing-document items. Upload through the
  existing Import Center using the Material Data import flow.
- `uat_scenarios.csv`: expected results and review aid. **Do not upload this
  file.** Join each row to Open Items using `Entity` + `Document Number` +
  `Line Number`.
- `metadata.json`: seed, generation timestamp, source-reference counts, row
  counts, scenario distributions, and actual percentages.

Upload the two import CSVs separately in the Import Center and use its existing
preview and confirmation flow. Upload MaterialData before Open Items so exact
purchasing references are available for matching. The generator does not import
any file or write transaction records to the database. Source accounting CSV
`Cost Type`, `WHT Rate`, `WHT Amount`, and `WHT Posted` fields are blank; human
Final Cost Type decisions remain separate from imported accounting data.

## Scenario mix

- 100 invoices: 60 with one, 30 with two, and 10 with four eligible expense
  lines (160 eligible lines total).
- WHT by invoice: 85 approved positive-rate mappings, 10 approved zero-rate
  mappings explicitly representing No WHT, and 5 missing approved mappings.
- Posting by invoice: 80 correct positive-rate postings, 5 deliberate
  positive-rate discrepancies (3 incorrect amounts and 2 missing postings),
  10 zero-rate invoices without WHT postings, and 5 unresolved invoices
  without WHT postings.
- Material references across eligible expense lines: 152 exact matches, 5
  blank purchasing references, and 3 references intentionally absent from the
  MaterialData file.
- Ten additional zero-amount expense-account lines are ineligible by the
  application rule. Control account `210000` and WHT account `229220` are
  excluded by current GLEligibility settings.
- Eight invoices contain signed negative expense postings as synthetic credit
  notes. Documents use GBP, USD, and EUR. All invoices use USD group currency;
  company-code currencies vary by synthetic entity.

Positive and zero WHT mappings, rates, supplier IDs, countries, and Cost Types
come from the current database. Missing-mapping cases use existing supplier and
active Cost Type records and remain `REQUIRES_REVIEW`, not zero WHT.

Four positive-rate invoices have posting dates outside the mappings' effective
date range. The application currently ignores SAP Posting Date when selecting
the latest approved mapping. The current reference set has no key with more
than one approved version, so these rows exercise date independence with the
latest available version (v1); they cannot distinguish version ranking against
an older version.

## Calculation and currency notes

Line WHT uses signed document-currency expense amounts and the application's
Decimal `ROUND_HALF_UP` behavior. Invoice calculation is the rounded sum of
line determinations. Reconciliation follows the application formula:
`calculated WHT + signed posted WHT`; correct postings use the opposite sign.
Deliberate discrepancies differ by more than 0.01. Missing mappings keep
calculated WHT and the reconciliation difference blank/null and use pending /
provisional status.

Company-code and group-currency amounts use fixed synthetic illustrative
conversion factors recorded in `metadata.json`. They are not sourced FX rates
and are not used in WHT calculations.
"""

    def _write_csv(
        self,
        path: Path,
        headers: tuple[str, ...],
        rows: list[dict[str, str]],
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=headers, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)

    def _validate_written_files(
        self,
        output_dir: Path,
        dataset: GeneratedDataset,
    ) -> None:
        expected = {
            "open_line_items_uat.csv": (OPEN_ITEM_HEADERS, len(dataset.open_item_rows)),
            "material_data_uat.csv": (
                MATERIAL_DATA_HEADERS,
                len(dataset.material_data_rows),
            ),
            "uat_scenarios.csv": (SCENARIO_HEADERS, len(dataset.scenario_rows)),
        }
        for filename, (headers, row_count) in expected.items():
            with (output_dir / filename).open(
                newline="",
                encoding="utf-8",
            ) as source:
                reader = csv.DictReader(source)
                if tuple(reader.fieldnames or ()) != headers:
                    raise CommandError(f"Generated {filename} has incorrect headers.")
                if sum(1 for _ in reader) != row_count:
                    raise CommandError(f"Generated {filename} has an incorrect row count.")
