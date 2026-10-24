from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from core.views import home
from transactions.views import (
    dashboard,
    complete_classification_view,
    complete_review_view,
    finalize_invoice_view,
    determine_wht_view,
    open_item_detail,
    open_item_document,
    open_item_list,
    export_open_items,
    export_review_completed_items,
    review_completed_items,
    return_item_view,
    start_review_view,
    submit_for_review_view,
    import_center,
    import_preview,
    import_batch_detail,
    exceptions_queue,
    exclude_line_view,
    flag_line_view,
    recalculate_wht_exception_view,
    resolve_line_flag_view,
    resolve_wht_exception_view,
    reopen_invoice_view,
    reopen_line_view,
)


urlpatterns = [
    path("", home, name="home"),
    path(
        "login/",
        LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("dashboard/", dashboard, name="dashboard"),
    path("exceptions/", exceptions_queue, name="exceptions-queue"),
    path("imports/", import_center, name="import-center"),
    path(
        "imports/preview/<uuid:token>/",
        import_preview,
        name="import-preview",
    ),
    path(
        "imports/batches/<int:pk>/",
        import_batch_detail,
        name="import-batch-detail",
    ),
    path("open-items/", open_item_list, name="open-item-list"),
    path("open-items/export.xlsx", export_open_items, name="open-item-export"),
    path(
        "review-completed/",
        review_completed_items,
        name="review-completed-items",
    ),
    path(
        "review-completed/export.xlsx",
        export_review_completed_items,
        name="review-completed-export",
    ),
    path("open-items/<int:pk>/determine-wht/", determine_wht_view, name="determine-wht"),
    path("open-items/<int:pk>/complete/", complete_classification_view, name="complete-classification"),
    path("open-items/<int:pk>/submit-review/", submit_for_review_view, name="submit-for-review"),
    path("open-items/<int:pk>/start-review/", start_review_view, name="start-review"),
    path("open-items/<int:pk>/complete-review/", complete_review_view, name="complete-review"),
    path("open-items/<int:pk>/finalize-invoice/", finalize_invoice_view, name="finalize-invoice"),
    path("open-items/<int:pk>/return/", return_item_view, name="return-item"),
    path("open-items/<int:pk>/reopen-line/", reopen_line_view, name="reopen-line"),
    path(
        "open-items/<int:pk>/reopen-invoice/",
        reopen_invoice_view,
        name="reopen-invoice",
    ),
    path("open-items/<int:pk>/flag/", flag_line_view, name="flag-line"),
    path("open-items/<int:pk>/exclude/", exclude_line_view, name="exclude-line"),
    path(
        "open-items/<int:pk>/resolve-wht-exception/",
        resolve_wht_exception_view,
        name="resolve-wht-exception",
    ),
    path(
        "open-items/<int:pk>/recalculate-wht/",
        recalculate_wht_exception_view,
        name="recalculate-wht",
    ),
    path(
        "exceptions/flags/<int:record_id>/resolve/",
        resolve_line_flag_view,
        name="resolve-line-flag",
    ),
    path(
        "open-items/<str:entity_code>/<str:accounting_document_number>/",
        open_item_document,
        name="open-item-document",
    ),
    path("open-items/<int:pk>/", open_item_detail, name="open-item-detail"),
    path("admin/", admin.site.urls),
]
