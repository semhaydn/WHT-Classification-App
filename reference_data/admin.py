from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ValidationError

from transactions.permissions import has_role

from .models import CostType, SupplierCountry, WHTMapping
from .services import approve_wht_rule


class AdministratorOnlyModelAdmin(admin.ModelAdmin):
    def _is_administrator(self, request) -> bool:
        return has_role(request.user, "Administrator")

    def has_module_permission(self, request) -> bool:
        return self._is_administrator(request)

    def has_view_permission(self, request, obj=None) -> bool:
        return self._is_administrator(request)

    def has_add_permission(self, request) -> bool:
        return self._is_administrator(request)

    def has_change_permission(self, request, obj=None) -> bool:
        return self._is_administrator(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        return self._is_administrator(request)


@admin.register(CostType)
class CostTypeAdmin(AdministratorOnlyModelAdmin):
    list_display = ("code", "name", "active", "updated_at")
    list_filter = ("active",)
    search_fields = ("code", "name")


@admin.register(SupplierCountry)
class SupplierCountryAdmin(AdministratorOnlyModelAdmin):
    list_display = ("supplier_id", "supplier_name", "country_code", "updated_at")
    list_filter = ("country_code",)
    search_fields = ("supplier_id", "supplier_name")


@admin.register(WHTMapping)
class WHTMappingAdmin(AdministratorOnlyModelAdmin):
    list_display = (
        "supplier",
        "cost_category",
        "country_code",
        "version",
        "effective_from",
        "effective_to",
        "status",
        "wht_rate",
    )
    list_filter = ("status", "country_code", "cost_category")
    search_fields = ("supplier", "supplier_name", "source_title")
    readonly_fields = ("status", "approved_by", "approved_at", "source_row_number")
    actions = ("approve_selected_rules",)

    def has_change_permission(self, request, obj=None) -> bool:
        return self._is_administrator(request) and (
            obj is None or obj.status == WHTMapping.Status.DRAFT
        )

    def has_delete_permission(self, request, obj=None) -> bool:
        return self._is_administrator(request) and (
            obj is None or obj.status == WHTMapping.Status.DRAFT
        )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == WHTMapping.Status.APPROVED:
            return tuple(field.name for field in self.model._meta.fields)
        return self.readonly_fields

    @admin.action(description="Approve selected draft WHT rules")
    def approve_selected_rules(self, request, queryset) -> None:
        approved = 0
        for rule in queryset:
            try:
                approve_wht_rule(rule.pk, request.user)
            except (ValidationError, ValueError) as error:
                self.message_user(
                    request,
                    f"Could not approve {rule}: {error}",
                    level=messages.ERROR,
                )
            else:
                approved += 1
        if approved:
            self.message_user(
                request,
                f"Approved {approved} WHT rule(s).",
                level=messages.SUCCESS,
            )

    def delete_queryset(self, request, queryset) -> None:
        if queryset.exclude(status=WHTMapping.Status.DRAFT).exists():
            self.message_user(
                request,
                "Approved WHT rules cannot be deleted.",
                level=messages.ERROR,
            )
            return
        queryset.delete()
