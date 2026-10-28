"""目标功能测试：订单状态筛选与金额下界筛选。

对应规格：SPEC-ORDER-QUERY-01 / SPEC-ORDER-QUERY-02 / SPEC-ORDER-QUERY-05。

本文件在"代码修改前"随规格一起引入，用于证明目标行为在修改前尚未实现
（用例会以断言失败，而不是环境错误失败）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fulfillment.errors import InvalidFilterError
from fulfillment.models import Order
from fulfillment.query import list_orders


def _order(order_id: str, status: str, amount: str) -> Order:
    return Order(
        order_id=order_id,
        customer_id="C-1",
        status=status,
        amount=Decimal(amount),
        created_at=date(2026, 1, 1),
    )


ORDERS = [
    _order("O-1", "PAID", "100.00"),
    _order("O-2", "SHIPPED", "250.50"),
    _order("O-3", "PAID", "80.00"),
    _order("O-4", "CANCELLED", "500.00"),
]


def test_status_filter_returns_only_matching_orders():
    """SPEC-ORDER-QUERY-01：按 status 过滤后只保留状态匹配的订单。"""
    result = list_orders(ORDERS, status="PAID")
    assert [o.order_id for o in result] == ["O-1", "O-3"]


def test_status_filter_rejects_unknown_status():
    """SPEC-ORDER-QUERY-01：未知状态必须抛 InvalidFilterError，而不是静默返回空列表。"""
    with pytest.raises(InvalidFilterError):
        list_orders(ORDERS, status="NOT_A_STATUS")


def test_status_filter_none_returns_all_orders():
    """SPEC-ORDER-QUERY-01：status 为 None 时不过滤（既有行为必须保持）。"""
    result = list_orders(ORDERS, status=None)
    assert [o.order_id for o in result] == ["O-1", "O-2", "O-3", "O-4"]


def test_min_amount_filter_is_inclusive():
    """SPEC-ORDER-QUERY-02：min_amount 按"大于等于"语义过滤。"""
    result = list_orders(ORDERS, min_amount=Decimal("100.00"))
    assert [o.order_id for o in result] == ["O-1", "O-2", "O-4"]


def test_min_amount_zero_is_equivalent_to_no_filter():
    """SPEC-ORDER-QUERY-02：min_amount == 0 等价于不过滤（既有行为必须保持）。"""
    result = list_orders(ORDERS, min_amount=Decimal("0"))
    assert [o.order_id for o in result] == ["O-1", "O-2", "O-3", "O-4"]


def test_min_amount_negative_is_rejected():
    """SPEC-ORDER-QUERY-02：负数金额下界必须抛 InvalidFilterError。"""
    with pytest.raises(InvalidFilterError):
        list_orders(ORDERS, min_amount=Decimal("-1"))


def test_combined_filters_are_intersection():
    """SPEC-ORDER-QUERY-05：status 与 min_amount 之间是"与"关系。"""
    result = list_orders(ORDERS, status="PAID", min_amount=Decimal("90.00"))
    assert [o.order_id for o in result] == ["O-1"]
