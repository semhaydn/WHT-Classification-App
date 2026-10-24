from collections.abc import Iterable

from transactions.models import (
    GLEligibility,
    LineExceptionRecord,
    OpenItem,
)


def get_gl_eligibility_map(gl_accounts: Iterable[str]) -> dict[str, bool]:
    configurations = GLEligibility.objects.filter(
        gl_account__in=set(gl_accounts)
    ).values_list("gl_account", "is_eligible")
    return dict(configurations)


def is_gl_account_eligible(gl_account: str) -> bool:
    return get_gl_eligibility_map((gl_account,)).get(gl_account, True)


def is_open_item_classifiable(
    open_item: OpenItem,
    configured_eligibility: dict[str, bool] | None = None,
    excluded_item_ids: set[int] | None = None,
) -> bool:
    if open_item.document_currency_amount == 0:
        return False
    if excluded_item_ids is None:
        excluded = LineExceptionRecord.objects.filter(
            open_item=open_item,
            status=LineExceptionRecord.Status.EXCLUDED,
        ).exists()
    else:
        excluded = open_item.pk in excluded_item_ids
    if excluded:
        return False
    if configured_eligibility is None:
        return is_gl_account_eligible(open_item.gl_account)
    return configured_eligibility.get(open_item.gl_account, True)
