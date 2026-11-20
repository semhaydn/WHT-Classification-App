from django.contrib import admin

from .permissions import has_role
from .models import (
    AuditEvent,
    CostTypeAISettings,
    CostTypeRecommendation,
    ClassificationDecision,
    GLEligibility,
    LineExceptionRecord,
    MaterialData,
    MaterialMatch,
    OpenItem,
    ReviewAction,
    ReviewDocumentSnapshot,
    ReviewLineCalculationSnapshot,
    WHTDetermination,
)


class ReadOnlyWorkflowAdmin(admin.ModelAdmin):
    def has_module_permission(self, request) -> bool:
        return has_role(request.user, "Transaction User", "Administrator")

    def has_view_permission(self, request, obj=None) -> bool:
        return self.has_module_permission(request)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(CostTypeAISettings)
class CostTypeAISettingsAdmin(admin.ModelAdmin):
    fields = ("enabled",)

    def has_module_permission(self, request) -> bool:
        return has_role(request.user, "Administrator")

    def has_view_permission(self, request, obj=None) -> bool:
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None) -> bool:
        return self.has_module_permission(request)

    def has_add_permission(self, request) -> bool:
        return self.has_module_permission(request) and not CostTypeAISettings.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def save_model(self, request, obj, form, change) -> None:
        obj.pk = 1
        super().save_model(request, obj, form, change)


@admin.register(CostTypeRecommendation)
class CostTypeRecommendationAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "model_identifier", "created_at")
    search_fields = ("open_item__accounting_document_number",)


@admin.register(GLEligibility)
class GLEligibilityAdmin(admin.ModelAdmin):
    list_display = ("gl_account", "is_eligible", "description", "updated_at")
    list_filter = ("is_eligible",)
    search_fields = ("gl_account", "description")
    fields = ("gl_account", "is_eligible", "description")

    def _can_manage(self, request) -> bool:
        return has_role(request.user, "Administrator")

    def has_module_permission(self, request) -> bool:
        return self._can_manage(request)

    def has_view_permission(self, request, obj=None) -> bool:
        return self._can_manage(request)

    def has_add_permission(self, request) -> bool:
        return self._can_manage(request)

    def has_change_permission(self, request, obj=None) -> bool:
        return self._can_manage(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        return self._can_manage(request)


@admin.register(LineExceptionRecord)
class LineExceptionRecordAdmin(ReadOnlyWorkflowAdmin):
    list_display = (
        "open_item",
        "issue_type",
        "status",
        "created_by",
        "created_at",
        "resolved_by",
        "resolved_at",
    )
    list_filter = ("issue_type", "status")
    search_fields = (
        "open_item__accounting_document_number",
        "open_item__line_number",
        "reason",
        "resolution_decision",
    )


@admin.register(OpenItem)
class OpenItemAdmin(ReadOnlyWorkflowAdmin):
    list_display = (
        "entity_code",
        "accounting_document_number",
        "line_number",
        "vendor_number",
        "document_currency_amount",
        "status",
    )
    list_filter = ("status", "entity_code", "document_currency")
    search_fields = (
        "accounting_document_number",
        "document_reference",
        "vendor_number",
    )


@admin.register(MaterialData)
class MaterialDataAdmin(admin.ModelAdmin):
    list_display = ("material_data_key", "purchasing_document", "item", "material", "plant")
    search_fields = ("material_data_key", "purchasing_document", "item", "material")

    def _can_maintain_material_data(self, request) -> bool:
        return has_role(request.user, "Administrator")

    def has_module_permission(self, request) -> bool:
        return self._can_maintain_material_data(request)

    def has_view_permission(self, request, obj=None) -> bool:
        return self._can_maintain_material_data(request)

    def has_add_permission(self, request) -> bool:
        return self._can_maintain_material_data(request)

    def has_change_permission(self, request, obj=None) -> bool:
        return self._can_maintain_material_data(request)

    def has_delete_permission(self, request, obj=None) -> bool:
        return self._can_maintain_material_data(request)


@admin.register(MaterialMatch)
class MaterialMatchAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "status", "candidate_count", "material_data", "created_at")
    list_filter = ("status",)


@admin.register(ClassificationDecision)
class ClassificationDecisionAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "final_cost_type", "is_current", "decided_at")
    list_filter = ("is_current", "classification_method")


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "event_type", "performed_by", "created_at")
    list_filter = ("event_type",)
    readonly_fields = (
        "open_item",
        "event_type",
        "performed_by",
        "previous_value",
        "new_value",
        "comment",
        "created_at",
    )


@admin.register(ReviewAction)
class ReviewActionAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "action_type", "performed_by", "created_at")
    list_filter = ("action_type",)
    readonly_fields = (
        "open_item",
        "action_type",
        "performed_by",
        "comments",
        "override_reason",
        "created_at",
    )


@admin.register(WHTDetermination)
class WHTDeterminationAdmin(ReadOnlyWorkflowAdmin):
    list_display = ("open_item", "status", "wht_rate_snapshot", "wht_amount_snapshot", "calculated_at")
    list_filter = ("status", "currency_snapshot")
    readonly_fields = (
        "open_item",
        "status",
        "candidate_count",
        "matched_mapping",
        "final_cost_type_snapshot",
        "supplier_snapshot",
        "supplier_country_snapshot",
        "rule_snapshot",
        "rule_version_snapshot",
        "wht_rate_snapshot",
        "wht_base_snapshot",
        "wht_amount_snapshot",
        "currency_snapshot",
        "calculated_at",
    )


@admin.register(ReviewDocumentSnapshot)
class ReviewDocumentSnapshotAdmin(ReadOnlyWorkflowAdmin):
    list_display = (
        "entity_code",
        "accounting_document_number",
        "calculation_version",
        "is_current",
        "review_status",
        "completed_by",
        "completed_at",
    )
    list_filter = ("is_current", "review_status", "calculation_version")
    search_fields = ("entity_code", "accounting_document_number", "supplier")


@admin.register(ReviewLineCalculationSnapshot)
class ReviewLineCalculationSnapshotAdmin(ReadOnlyWorkflowAdmin):
    list_display = (
        "document_snapshot",
        "line_number",
        "final_cost_type",
        "wht_rate",
        "calculated_line_wht",
    )
    list_filter = ("document_snapshot__is_current",)
    search_fields = (
        "document_snapshot__entity_code",
        "document_snapshot__accounting_document_number",
        "line_number",
    )
