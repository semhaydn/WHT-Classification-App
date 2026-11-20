import copy
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from reference_data.models import SupplierCountry, WHTMapping
from reference_data.services import approve_wht_rule


EXPECTED_APPROVED_COUNT = 0
EXPECTED_DRAFT_COUNT = 47


@dataclass
class CandidateValidation:
    rule: WHTMapping
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Command(BaseCommand):
    help = "Safely approve the expected set of draft WHT mappings."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--effective-from", required=True)
        parser.add_argument("--effective-to", required=True)
        parser.add_argument("--expected-count", type=int, required=True)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Approve the validated batch; without this option, only preview it.",
        )

    def handle(self, *args, **options) -> None:
        effective_from = self._parse_date(
            options["effective_from"],
            "--effective-from",
        )
        effective_to = self._parse_date(options["effective_to"], "--effective-to")
        if effective_to < effective_from:
            raise CommandError("--effective-to must not be earlier than --effective-from.")
        if options["expected_count"] != EXPECTED_DRAFT_COUNT:
            raise CommandError(
                f"--expected-count must be {EXPECTED_DRAFT_COUNT} for this approved "
                "batch."
            )

        administrator = self._get_administrator(options["user_id"])
        drafts = self._get_drafts()
        self.stdout.write(
            "Mapping counts: "
            f"total={WHTMapping.objects.count()} "
            f"approved={WHTMapping.objects.filter(status=WHTMapping.Status.APPROVED).count()} "
            f"draft={len(drafts)}"
        )
        self._check_expected_counts(len(drafts))
        validations = self._validate_candidates(
            drafts,
            administrator,
            effective_from,
            effective_to,
        )
        self._print_candidates(validations, effective_from, effective_to)
        self._print_batch_result(validations)

        if any(result.errors for result in validations):
            raise CommandError("Preflight failed; no mappings were changed.")
        if not options["confirm"]:
            self.stdout.write("Dry run only. No mappings were changed.")
            return

        approved_ids: list[int] = []
        try:
            with transaction.atomic():
                locked_drafts = list(
                    WHTMapping.objects.select_for_update()
                    .filter(status=WHTMapping.Status.DRAFT)
                    .select_related("cost_category")
                    .order_by("pk")
                )
                self._check_expected_counts(len(locked_drafts))
                if [rule.pk for rule in locked_drafts] != [
                    rule.pk for rule in drafts
                ]:
                    raise CommandError(
                        "The draft set changed after preflight; no mappings were changed."
                    )

                locked_validations = self._validate_candidates(
                    locked_drafts,
                    administrator,
                    effective_from,
                    effective_to,
                )
                if any(result.errors for result in locked_validations):
                    raise CommandError(
                        "Preflight changed before approval; no mappings were changed."
                    )

                for rule in locked_drafts:
                    rule.effective_from = effective_from
                    rule.effective_to = effective_to
                    rule.save(update_fields=["effective_from", "effective_to"])
                    approve_wht_rule(rule.pk, administrator)
                    approved_ids.append(rule.pk)

                if (
                    WHTMapping.objects.count()
                    != EXPECTED_APPROVED_COUNT + EXPECTED_DRAFT_COUNT
                    or WHTMapping.objects.filter(
                        status=WHTMapping.Status.APPROVED
                    ).count()
                    != EXPECTED_APPROVED_COUNT + EXPECTED_DRAFT_COUNT
                    or WHTMapping.objects.filter(
                        status=WHTMapping.Status.DRAFT
                    ).exists()
                ):
                    raise CommandError(
                        "Final mapping counts did not match expectations; "
                        "the batch was rolled back."
                    )
        except (IntegrityError, PermissionDenied, ValidationError, ValueError) as error:
            self.stderr.write(f"Approval failed: {error}")
            self.stdout.write("No mappings were committed; the batch was rolled back.")
            for rule in drafts:
                self.stdout.write(f"  ID {rule.pk}: rolled back")
            raise CommandError("The approval batch was rolled back.") from error

        approved_count = WHTMapping.objects.filter(
            status=WHTMapping.Status.APPROVED
        ).count()
        draft_count = WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Final mappings: total={WHTMapping.objects.count()} "
                f"approved={approved_count} draft={draft_count}"
            )
        )
        self.stdout.write("Per-rule outcomes:")
        for rule_id in approved_ids:
            self.stdout.write(f"  ID {rule_id}: approved")

    def _parse_date(self, value: str, option: str) -> date:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise CommandError(f"{option} must be a valid YYYY-MM-DD date.") from error
        if parsed.isoformat() != value:
            raise CommandError(f"{option} must use YYYY-MM-DD format.")
        return parsed

    def _get_administrator(self, user_id: int):
        user_model = get_user_model()
        eligible_users = user_model.objects.filter(
            Q(is_superuser=True) | Q(is_staff=True, groups__name="Administrator")
        ).distinct()
        self.stdout.write("Eligible Administrator users (ID, username):")
        for user in eligible_users.order_by("pk"):
            self.stdout.write(f"  {user.pk}, {user.get_username()}")

        try:
            user = user_model.objects.get(pk=user_id)
        except user_model.DoesNotExist as error:
            raise CommandError(f"User ID {user_id} does not exist.") from error

        if not (
            user.is_superuser
            or (
                user.is_staff
                and user.groups.filter(name="Administrator").exists()
            )
        ):
            raise CommandError(
                f"User ID {user_id} is not an authorized Administrator."
            )
        return user

    def _get_drafts(self) -> list[WHTMapping]:
        return list(
            WHTMapping.objects.filter(status=WHTMapping.Status.DRAFT)
            .select_related("cost_category")
            .order_by("pk")
        )

    def _check_expected_counts(self, draft_count: int) -> None:
        approved_count = WHTMapping.objects.filter(
            status=WHTMapping.Status.APPROVED
        ).count()
        total_count = WHTMapping.objects.count()
        if (
            total_count != EXPECTED_APPROVED_COUNT + EXPECTED_DRAFT_COUNT
            or approved_count != EXPECTED_APPROVED_COUNT
            or draft_count != EXPECTED_DRAFT_COUNT
        ):
            raise CommandError(
                "Mapping count check failed: expected "
                f"total={EXPECTED_APPROVED_COUNT + EXPECTED_DRAFT_COUNT}, "
                f"approved={EXPECTED_APPROVED_COUNT}, "
                f"draft={EXPECTED_DRAFT_COUNT}; found total={total_count}, "
                f"approved={approved_count}, draft={draft_count}."
            )

    def _validate_candidates(
        self,
        drafts: list[WHTMapping],
        administrator,
        effective_from: date,
        effective_to: date,
    ) -> list[CandidateValidation]:
        validations = [CandidateValidation(rule) for rule in drafts]
        suppliers = {
            supplier.supplier_id: supplier
            for supplier in SupplierCountry.objects.filter(
                supplier_id__in={rule.supplier for rule in drafts}
            )
        }
        by_key: dict[tuple[str, str, int], list[CandidateValidation]] = defaultdict(list)

        for result in validations:
            rule = result.rule
            supplier = suppliers.get(rule.supplier)
            if supplier is None:
                result.errors.append(
                    f"Supplier {rule.supplier} does not exist in SupplierCountry."
                )
            elif supplier.country_code != rule.country_code:
                result.errors.append(
                    f"Mapping country {rule.country_code} does not match "
                    f"SupplierCountry country {supplier.country_code} for "
                    f"{rule.supplier}."
                )
            if not rule.cost_category.active:
                result.warnings.append(
                    f"Cost Type {rule.cost_category.code} is inactive."
                )

            try:
                rule.full_clean()
            except ValidationError as error:
                result.errors.extend(error.messages)

            validation_rule = copy.copy(rule)
            validation_rule._state = copy.copy(rule._state)
            validation_rule.effective_from = effective_from
            validation_rule.effective_to = effective_to
            validation_rule.status = WHTMapping.Status.APPROVED
            validation_rule.approved_by = administrator
            validation_rule.approved_at = timezone.now()
            try:
                validation_rule.full_clean()
            except ValidationError as error:
                result.errors.extend(error.messages)

            key = (rule.supplier, rule.country_code, rule.cost_category_id)
            by_key[key].append(result)

        for group in by_key.values():
            for index, first in enumerate(group):
                for second in group[index + 1 :]:
                    if self._periods_overlap(
                        effective_from,
                        effective_to,
                        effective_from,
                        effective_to,
                    ):
                        first.errors.append(
                            f"Proposed period conflicts with draft mapping ID "
                            f"{second.rule.pk} for the same supplier, country, "
                            "and Cost Type."
                        )
                        second.errors.append(
                            f"Proposed period conflicts with draft mapping ID "
                            f"{first.rule.pk} for the same supplier, country, "
                            "and Cost Type."
                        )
        return validations

    def _periods_overlap(
        self,
        first_from: date,
        first_to: date | None,
        second_from: date,
        second_to: date | None,
    ) -> bool:
        return (
            (first_to is None or second_from <= first_to)
            and (second_to is None or first_from <= second_to)
        )

    def _print_candidates(
        self,
        validations: list[CandidateValidation],
        effective_from: date,
        effective_to: date,
    ) -> None:
        self.stdout.write("Draft mapping candidates:")
        for result in validations:
            rule = result.rule
            self.stdout.write(
                f"  ID={rule.pk} supplier={rule.supplier} "
                f"country={rule.country_code} cost_type={rule.cost_category.code} "
                f"version={rule.version} rate={rule.wht_rate} "
                f"effective_from={effective_from} effective_to={effective_to}"
            )
            for error in result.errors:
                self.stderr.write(f"    ERROR: {error}")
            for warning in result.warnings:
                self.stderr.write(f"    WARNING: {warning}")

    def _print_batch_result(
        self,
        validations: list[CandidateValidation],
    ) -> None:
        valid_count = sum(not result.errors for result in validations)
        if any(result.errors for result in validations):
            self.stdout.write(
                f"Batch blocked: {valid_count}/{len(validations)} candidates passed; "
                "0 would be approved atomically."
            )
        else:
            self.stdout.write(
                f"Preflight passed: exactly {len(validations)} mappings would be approved."
            )
