from datetime import date

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class CostType(models.Model):
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.name


class SupplierCountry(models.Model):
    supplier_id = models.CharField(max_length=100, unique=True)
    country_code = models.CharField(max_length=2)
    supplier_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["supplier_id"]

    def __str__(self) -> str:
        return f"{self.supplier_id} - {self.country_code}"


class WHTMapping(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"

    supplier = models.CharField(max_length=100)
    supplier_name = models.CharField(max_length=200)
    cost_category = models.ForeignKey(
        CostType,
        on_delete=models.PROTECT,
        related_name="wht_mappings",
    )
    wht_rate = models.DecimalField(max_digits=7, decimal_places=6)
    country_code = models.CharField(max_length=2)
    source_title = models.CharField(max_length=200)
    source_row_number = models.PositiveIntegerField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    approved_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_wht_mappings",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "country_code", "cost_category", "version"],
                name="unique_wht_rule_version",
            )
        ]
        ordering = ["supplier", "cost_category__code", "country_code", "version"]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.version < 1:
            errors["version"] = "Rule version must be a positive integer."
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            errors["effective_to"] = "Effective To must be on or after Effective From."
        if self.wht_rate < 0:
            errors["wht_rate"] = "WHT rate cannot be negative."
        if self.status == self.Status.APPROVED:
            if self.effective_from is None:
                errors["effective_from"] = "An effective-from date is required for approval."
            if self.approved_by_id is None:
                errors["approved_by"] = "An approving administrator is required."
            if self.approved_at is None:
                errors["approved_at"] = "An approval timestamp is required."
            if self.effective_from is not None:
                overlaps = WHTMapping.objects.filter(
                    supplier=self.supplier,
                    country_code=self.country_code,
                    cost_category=self.cost_category,
                    status=self.Status.APPROVED,
                    effective_from__lte=self.effective_to or date.max,
                ).filter(
                    Q(effective_to__isnull=True)
                    | Q(effective_to__gte=self.effective_from)
                )
                if self.pk:
                    overlaps = overlaps.exclude(pk=self.pk)
                if overlaps.exists():
                    errors["effective_from"] = (
                        "This rule overlaps an approved rule for the same supplier, "
                        "country, and Cost Type."
                    )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs) -> None:
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status == self.Status.APPROVED:
                changed_fields = [
                    field.name
                    for field in self._meta.concrete_fields
                    if field.name != "id"
                    and getattr(previous, field.attname) != getattr(self, field.attname)
                ]
                if changed_fields:
                    raise ValidationError(
                        "Approved WHT rules are immutable; create a new version."
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.APPROVED:
            raise ValidationError("Approved WHT rules cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.supplier} - {self.cost_category.code} - "
            f"{self.country_code} - v{self.version}"
        )
