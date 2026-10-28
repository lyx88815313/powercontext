"""订单列表查询。

规格来源：SPEC-ORDER-QUERY（``specs/SPEC-ORDER-QUERY.md``）。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .errors import InvalidFilterError
from .models import Order

ALLOWED_STATUSES = frozenset({"PAID", "SHIPPED", "CANCELLED", "REFUNDED"})


def list_orders(
    orders: Iterable[Order],
    status: Optional[str] = None,
    min_amount: Optional[float] = None,
) -> Sequence[Order]:
    """按状态与金额下界筛选订单。

    Args:
        orders: 待筛选的订单序列。
        status: 订单状态过滤值；``None`` 表示不过滤。
        min_amount: 金额下界过滤值；``None`` 表示不过滤。

    Returns:
        筛选后的订单序列，顺序与入参一致。

    Raises:
        InvalidFilterError: 状态或金额下界非法。
    """
    if status is not None and status not in ALLOWED_STATUSES:
        raise InvalidFilterError(f"unknown order status: {status}")
    if min_amount is not None and min_amount < 0:
        raise InvalidFilterError(f"min_amount must be non-negative, got {min_amount}")

    result = []
    for order in orders:
        if status is not None and order.status != status:
            continue
        if min_amount is not None and min_amount != 0 and order.amount < min_amount:
            continue
        result.append(order)
    return result
