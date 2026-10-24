from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q

from reference_data.models import CostType


class ImportUploadSession(models.Model):
    class Status(models.TextChoices):
        READY = "READY", "Ready to import"
        IMPORTING = "IMPORTING", "Import in progress"
        COMPLETED = "COMPLETED", "Completed"
        INVALID = "INVALID", "Needs correction"
        EXPIRED = "EXPIRED", "Expired"

    token = models.UUIDField(unique=True)
    uploaded_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_upload_sessions",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.READY,
    )
    material_staged_name = models.CharField(max_length=64, blank=True)
    material_original_name = models.CharField(max_length=255, blank=True)
    material_sha256 = models.CharField(max_length=64, blank=True)
    open_items_staged_name = models.CharField(max_length=64, blank=True)
    open_items_original_name = models.CharField(max_length=255, blank=True)
    open_items_sha256 = models.CharField(max_length=64, blank=True)
    preview = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ImportBatch(models.Model):
    class ImportType(models.TextChoices):
        MATERIAL_DATA = "MATERIAL_DATA", "Material data"
        HISTORICAL_COST_TYPES = "HISTORICAL_COST_TYPES", "Historical Cost Types"
        OPEN_ITEMS = "OPEN_ITEMS", "Open items"

    import_type = models.CharField(max_length=30, choices=ImportType.choices)
    source_filename = models.CharField(max_length=255)
    source_sha256 = models.CharField(max_length=64)
    rows_read = models.PositiveIntegerField(default=0)
    rows_created = models.PositiveIntegerField(default=0)
    rows_updated = models.PositiveIntegerField(default=0)
    rows_skipped = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_import_batches",
    )
    upload_session = models.ForeignKey(
        ImportUploadSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_batches",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["import_type", "source_sha256"],
                name="unique_import_batch_source",
            )
        ]
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_import_type_display()} — {self.source_filename}"


class ImportValidationError(models.Model):
    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="validation_errors",
    )
    row_number = models.PositiveIntegerField()
    message = models.TextField()
    raw_row = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["row_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="unique_import_error_row",
            )
        ]


class HistoricalCostTypeSuggestion(models.Model):
    source_document_number = models.CharField(max_length=100)
    source_item_number = models.CharField(max_length=50)
    suggested_cost_type = models.CharField(max_length=150, blank=True)
    confidence_rate = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    prediction_basis = models.TextField(blank=True)
    source_row_number = models.PositiveIntegerField()
    source_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="historical_cost_type_suggestions",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_batch", "source_row_number"],
                name="unique_historical_suggestion_source_row",
            )
        ]
        ordering = ["source_document_number", "source_item_number", "source_row_number"]


class MaterialData(models.Model):
    material_data_key = models.CharField(max_length=150)
    purchasing_document = models.CharField(max_length=100)
    item = models.CharField(max_length=50)
    material = models.CharField(max_length=150, blank=True)
    plant = models.CharField(max_length=50)
    order_unit = models.CharField(max_length=20)
    document_date = models.DateField()
    supplier_or_supplying_plant = models.CharField(max_length=255)
    short_text = models.TextField()
    purchasing_organization = models.CharField(max_length=50)
    net_price = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    net_order_value = models.DecimalField(max_digits=18, decimal_places=2)
    purchasing_info_record = models.CharField(max_length=100, blank=True)
    purchasing_document_type = models.CharField(max_length=20)
    purchasing_document_category = models.CharField(max_length=20)
    purchasing_group = models.CharField(max_length=50)
    material_group = models.CharField(max_length=100)
    source_import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="material_data_rows",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["purchasing_document", "item"],
                name="unique_material_document_item",
            )
        ]
        ordering = ["purchasing_document", "item", "id"]

    def __str__(self) -> str:
        return self.material_data_key


class OpenItem(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        IN_CLASSIFICATION = "IN_CLASSIFICATION", "In classification"
        COMPLETED = "COMPLETED", "Completed"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        REVIEWED = "REVIEWED", "Reviewed"
        RETURNED = "RETURNED", "Returned"

    title = models.CharField(max_length=100)
    entity_code = models.CharField(max_length=50)
    accounting_document_number = models.CharField(max_length=100)
    line_number = models.CharField(max_length=50)
    document_type = models.CharField(max_length=20)
    document_date = models.DateField()
    posting_date = models.DateField()
    document_reference = models.CharField(max_length=100)
    vendor_number = models.CharField(max_length=100, blank=True)
    vendor_name = models.CharField(max_length=200, blank=True)
    purchasing_document = models.CharField(max_length=100, blank=True)
    purchasing_document_item = models.CharField(max_length=50, blank=True)
    material_data_key = models.CharField(max_length=150, blank=True)
    material_group = models.CharField(max_length=100, blank=True)
    material = models.CharField(max_length=150, blank=True)
    material_description = models.CharField(max_length=255, blank=True)
    document_currency_amount = models.DecimalField(max_digits=18, decimal_places=2)
    document_currency = models.CharField(max_length=3)
    company_code_currency_amount = models.DecimalField(max_digits=18, decimal_places=2)
    company_code_currency = models.CharField(max_length=3)
    group_currency_amount = models.DecimalField(max_digits=18, decimal_places=2)
    group_currency = models.CharField(max_length=3)
    gl_account = models.CharField(max_length=50)
    gl_account_description = models.CharField(max_length=255)
    offsetting_account = models.CharField(max_length=100)
    text = models.TextField(blank=True)
    payment_block = models.CharField(max_length=20, blank=True)
    payment_method = models.CharField(max_length=20, blank=True)
    assignment = models.CharField(max_length=255, blank=True)
    net_due_date = models.DateField(null=True, blank=True)
    clearing_date = models.DateField(null=True, blank=True)
    payment_date = models.DateField(null=True, blank=True)
    clearing_document = models.CharField(max_length=100, blank=True)
    comments = models.TextField(blank=True)
    country = models.CharField(max_length=2, blank=True)
    source_import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="open_items",
    )
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.NEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity_code", "accounting_document_number", "line_number"],
                name="unique_open_item_business_key",
            )
        ]
        ordering = ["entity_code", "accounting_document_number", "line_number"]

    def __str__(self) -> str:
        return f"{self.entity_code}-{self.accounting_document_number}-{self.line_number}"

    @property
    def is_classifiable(self) -> bool:
        from transactions.services.gl_eligibility import is_open_item_classifiable

        return is_open_item_classifiable(self)


class GLEligibility(models.Model):
    gl_account = models.CharField(max_length=50, unique=True)
    is_eligible = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["gl_account"]
        verbose_name = "G/L eligibility"
        verbose_name_plural = "G/L eligibility"

    def __str__(self) -> str:
        state = "Eligible" if self.is_eligible else "Excluded"
        return f"{self.gl_account} — {state}"


class LineExceptionRecord(models.Model):
    class Type(models.TextChoices):
        USER_FLAG = "USER_FLAG", "Administrator review flag"
        ADMIN_EXCLUSION = "ADMIN_EXCLUSION", "Administrator line exclusion"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"
        EXCLUDED = "EXCLUDED", "Excluded"

    open_item = models.ForeignKey(
        OpenItem,
        on_delete=models.CASCADE,
        related_name="exception_records",
    )
    issue_type = models.CharField(max_length=30, choices=Type.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    reason = models.TextField()
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="created_line_exceptions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolution_decision = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_line_exceptions",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["open_item"],
                condition=Q(issue_type="USER_FLAG", status="OPEN"),
                name="one_open_admin_flag_per_line",
            ),
            models.UniqueConstraint(
                fields=["open_item"],
                condition=Q(issue_type="ADMIN_EXCLUSION", status="EXCLUDED"),
                name="one_active_line_exclusion",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.open_item} — {self.get_issue_type_display()} ({self.get_status_display()})"


class MaterialMatch(models.Model):
    class Status(models.TextChoices):
        NOT_FOUND = "NOT_FOUND", "Not found"
        MATCHED = "MATCHED", "Matched"
        AMBIGUOUS = "AMBIGUOUS", "Ambiguous"

    open_item = models.ForeignKey(OpenItem, on_delete=models.CASCADE, related_name="material_matches")
    material_data = models.ForeignKey(
        MaterialData,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="selected_matches",
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    candidate_count = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ClassificationDecision(models.Model):
    class Method(models.TextChoices):
        HUMAN = "HUMAN", "Human"
        HUMAN_OVERRIDE = "HUMAN_OVERRIDE", "Human override"
        IMPORT = "IMPORT", "Import"
        RULE = "RULE", "Rule"
        ML_SUGGESTION = "ML_SUGGESTION", "ML suggestion"

    open_item = models.ForeignKey(
        OpenItem,
        on_delete=models.CASCADE,
        related_name="classification_decisions",
    )
    suggested_cost_type = models.ForeignKey(
        CostType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="suggested_classifications",
    )
    final_cost_type = models.ForeignKey(
        CostType,
        on_delete=models.PROTECT,
        related_name="final_classifications",
    )
    classification_method = models.CharField(
        max_length=30,
        choices=Method.choices,
        default=Method.HUMAN,
    )
    classification_reason = models.TextField()
    decided_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="classification_decisions",
    )
    decided_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["open_item"],
                condition=models.Q(is_current=True),
                name="one_current_classification_per_item",
            )
        ]
        ordering = ["-decided_at", "-id"]


class AuditEvent(models.Model):
    class EventType(models.TextChoices):
        MATERIAL_MATCHED = "MATERIAL_MATCHED", "Material matched"
        MATERIAL_MATCH_NOT_FOUND = "MATERIAL_MATCH_NOT_FOUND", "Material match not found"
        MATERIAL_MATCH_AMBIGUOUS = "MATERIAL_MATCH_AMBIGUOUS", "Material match ambiguous"
        CLASSIFICATION_SELECTED = "CLASSIFICATION_SELECTED", "Classification selected"
        CLASSIFICATION_CHANGED = "CLASSIFICATION_CHANGED", "Classification changed"
        CLASSIFICATION_COMPLETED = "CLASSIFICATION_COMPLETED", "Classification completed"
        SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW", "Submitted for review"
        REVIEW_STARTED = "REVIEW_STARTED", "Review started"
        REVIEW_COMPLETED = "REVIEW_COMPLETED", "Review completed"
        APPROVED = "APPROVED", "Approved"
        RETURNED = "RETURNED", "Returned"
        ADMIN_REVIEW_FLAGGED = "ADMIN_REVIEW_FLAGGED", "Flagged for administrator review"
        ADMIN_REVIEW_CLEARED = "ADMIN_REVIEW_CLEARED", "Administrator review flag cleared"
        LINE_EXCLUDED = "LINE_EXCLUDED", "Accounting line excluded"
        WHT_EXCEPTION_RESOLVED = "WHT_EXCEPTION_RESOLVED", "WHT exception resolved"
        WHT_DETERMINATION_RECALCULATED = "WHT_DETERMINATION_RECALCULATED", "WHT determination recalculated"
        LINE_REOPENED = "LINE_REOPENED", "Accounting line reopened"
        INVOICE_REOPENED = "INVOICE_REOPENED", "Invoice reopened"
        INVOICE_RECOMPLETED = "INVOICE_RECOMPLETED", "Invoice recompleted"
        INVOICE_FINALIZED = "INVOICE_FINALIZED", "Invoice finalized"

    open_item = models.ForeignKey(
        OpenItem,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    performed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wht_audit_events",
    )
    previous_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class ReviewAction(models.Model):
    class ActionType(models.TextChoices):
        REVIEW_REQUESTED = "REVIEW_REQUESTED", "Review requested"
        SUBMITTED = "SUBMITTED", "Submitted"
        REVIEW_STARTED = "REVIEW_STARTED", "Review started"
        REVIEW_COMPLETED = "REVIEW_COMPLETED", "Review completed"
        APPROVED = "APPROVED", "Approved"
        RETURNED = "RETURNED", "Returned"

    open_item = models.ForeignKey(
        OpenItem,
        on_delete=models.CASCADE,
        related_name="review_actions",
    )
    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    performed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="review_actions",
    )
    comments = models.TextField(blank=True)
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WHTDetermination(models.Model):
    class Status(models.TextChoices):
        MATCHED = "MATCHED", "Matched"
        NO_WHT = "NO_WHT", "No WHT"
        NOT_FOUND = "NOT_FOUND", "Not found"
        AMBIGUOUS = "AMBIGUOUS", "Ambiguous"
        REQUIRES_REVIEW = "REQUIRES_REVIEW", "Requires review"
        ADMIN_RESOLVED = "ADMIN_RESOLVED", "Resolved by Administrator"

    open_item = models.ForeignKey(OpenItem, on_delete=models.CASCADE, related_name="wht_determinations")
    status = models.CharField(max_length=20, choices=Status.choices)
    candidate_count = models.PositiveIntegerField(default=0)
    matched_mapping = models.ForeignKey(
        "reference_data.WHTMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="determinations",
    )
    final_cost_type_snapshot = models.CharField(max_length=100)
    supplier_snapshot = models.CharField(max_length=100, blank=True)
    supplier_country_snapshot = models.CharField(max_length=2, blank=True)
    rule_snapshot = models.CharField(max_length=200, blank=True)
    rule_version_snapshot = models.CharField(max_length=100, blank=True)
    wht_rate_snapshot = models.DecimalField(max_digits=7, decimal_places=6, null=True, blank=True)
    wht_base_snapshot = models.DecimalField(max_digits=18, decimal_places=2)
    wht_amount_snapshot = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency_snapshot = models.CharField(max_length=3)
    rate_source = models.CharField(max_length=20, default="MAPPING")
    manual_override_reason = models.TextField(blank=True)
    calculation_version = models.PositiveIntegerField(default=1)
    calculated_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wht_calculations",
    )
    calculated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-calculated_at", "-id"]


class ReviewDocumentSnapshot(models.Model):
    class ReviewStatus(models.TextChoices):
        REVIEWED = "REVIEWED", "Reviewed"
        PARTIALLY_REVIEWED = "PARTIALLY_REVIEWED", "Partially reviewed"

    entity_code = models.CharField(max_length=50)
    accounting_document_number = models.CharField(max_length=100)
    supplier = models.CharField(max_length=100, blank=True)
    vendor_name = models.CharField(max_length=200, blank=True)
    supplier_country = models.CharField(max_length=2, blank=True)
    document_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    document_currency = models.CharField(max_length=3, blank=True)
    calculated_wht = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    booked_wht = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    has_wht_posting = models.BooleanField(default=False)
    modification_required = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    mapping_required = models.BooleanField(default=False)
    exception_reason = models.TextField(blank=True)
    review_status = models.CharField(max_length=30, choices=ReviewStatus.choices)
    completed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="completed_wht_document_snapshots",
    )
    completed_at = models.DateTimeField()
    source_import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_snapshots",
    )
    is_current = models.BooleanField(default=True)
    calculation_version = models.PositiveIntegerField(default=1)
    calculation_timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity_code", "accounting_document_number"],
                condition=Q(is_current=True),
                name="one_current_review_document_snapshot",
            )
        ]
        ordering = ["-completed_at", "entity_code", "accounting_document_number"]

    def __str__(self) -> str:
        return f"{self.entity_code}-{self.accounting_document_number} v{self.calculation_version}"


class ReviewLineCalculationSnapshot(models.Model):
    document_snapshot = models.ForeignKey(
        ReviewDocumentSnapshot,
        on_delete=models.CASCADE,
        related_name="line_snapshots",
    )
    open_item = models.ForeignKey(
        OpenItem,
        on_delete=models.CASCADE,
        related_name="review_calculation_snapshots",
    )
    line_number = models.CharField(max_length=50)
    supplier = models.CharField(max_length=100, blank=True)
    supplier_country = models.CharField(max_length=2, blank=True)
    final_cost_type = models.CharField(max_length=100, blank=True)
    eligible_service_amount = models.DecimalField(max_digits=18, decimal_places=2)
    document_currency = models.CharField(max_length=3)
    mapping = models.ForeignKey(
        "reference_data.WHTMapping",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="review_line_snapshots",
    )
    mapping_version = models.CharField(max_length=100, blank=True)
    wht_determination = models.ForeignKey(
        WHTDetermination,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="review_line_snapshots",
    )
    wht_rate = models.DecimalField(max_digits=7, decimal_places=6, null=True, blank=True)
    calculated_line_wht = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    exception_reason = models.TextField(blank=True)
    source_import_batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_line_snapshots",
    )

    class Meta:
        ordering = ["line_number", "id"]
