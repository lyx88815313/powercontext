"""回归测试：结算金额计算。

对应规格：SPEC-PRICING-01 / SPEC-PRICING-02 / SPEC-PRICING-03。
结算模块属于回归保护范围：订单查询的改造不得影响这里的任何行为。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fulfillment.errors import InvalidAmountError
from fulfillment.models import Order
from fulfillment.pricing import settlement_amount


def _order(order_id: str, amount: str) -> Order:
    return Order(
        order_id=order_id,
        customer_id="C-1",
        status="PAID",
        amount=Decimal(amount),
        created_at=date(2026, 1, 1),
    )


def test_settlement_without_discount():
    """100.00 订单，无折扣：折后 100.00，税 6.00，合计 106.00。"""
    assert settlement_amount(_order("O-1", "100.00")) == Decimal("106.00")


def test_settlement_with_discount_rounds_half_up():
    """折扣后金额按分四舍五入：99.99 * 0.9 = 89.991 -> 89.99，税 5.40，合计 95.39。"""
    total = settlement_amount(_order("O-1", "99.99"), Decimal("0.1"))
    assert total == Decimal("95.39")


def test_settlement_rejects_negative_amount():
    """SPEC-PRICING-01：负数订单金额必须抛异常。"""
    with pytest.raises(InvalidAmountError):
        settlement_amount(_order("O-1", "-1.00"))


@pytest.mark.parametrize("rate", ["-0.1", "1.0", "1.5"])
def test_settlement_rejects_invalid_discount_rate(rate):
    """SPEC-PRICING-02：折扣率必须落在 [0, 1)。"""
    with pytest.raises(InvalidAmountError):
        settlement_amount(_order("O-1", "100.00"), Decimal(rate))
