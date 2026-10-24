from collections.abc import Iterable
from decimal import Decimal

from transactions.models import OpenItem
from transactions.services.reconciliation import round_money


ZERO = Decimal("0.00")


def invoice_amount_from_control_lines(lines: Iterable[OpenItem]) -> Decimal | None:
    control_lines = [line for line in lines if line.gl_account == "210000"]
    if not control_lines or len({line.document_currency for line in control_lines}) != 1:
        return None
    return round_money(
        -sum((line.document_currency_amount for line in control_lines), ZERO)
    )
