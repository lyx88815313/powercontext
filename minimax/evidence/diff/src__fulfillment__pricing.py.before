"""结算金额计算。

规格来源：SPEC-PRICING（``specs/SPEC-PRICING.md``）。
本模块属于**回归保护范围**：任何订单查询改动都不允许影响这里的既有行为。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .errors import InvalidAmountError
from .models import Order

TAX_RATE = Decimal("0.06")
_CENT = Decimal("0.01")


def settlement_amount(order: Order, discount_rate: Decimal = Decimal("0")) -> Decimal:
    """计算订单结算金额（含税）。

    规则（SPEC-PRICING-01/02/03）：

    * 订单金额必须 >= 0，否则抛 ``InvalidAmountError``。
    * 折扣率必须落在 [0, 1) 区间内，否则抛 ``InvalidAmountError``。
    * 折后金额 = 订单金额 * (1 - 折扣率)，按分四舍五入。
    * 税额 = 折后金额 * 6%，按分四舍五入。
    * 结算金额 = 折后金额 + 税额，按分四舍五入。

    Args:
        order: 待结算订单。
        discount_rate: 折扣率，0 表示无折扣。

    Returns:
        两位小数的结算金额。

    Raises:
        InvalidAmountError: 金额或折扣率非法。
    """
    if order.amount < 0:
        raise InvalidAmountError(f"order {order.order_id} has negative amount: {order.amount}")
    if discount_rate < 0 or discount_rate >= 1:
        raise InvalidAmountError(f"discount_rate must be within [0, 1), got {discount_rate}")

    discounted = (order.amount * (Decimal("1") - discount_rate)).quantize(_CENT, rounding=ROUND_HALF_UP)
    tax = (discounted * TAX_RATE).quantize(_CENT, rounding=ROUND_HALF_UP)
    return (discounted + tax).quantize(_CENT, rounding=ROUND_HALF_UP)
