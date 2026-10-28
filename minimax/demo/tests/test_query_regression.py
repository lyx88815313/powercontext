"""回归测试：订单查询的既有行为在改造后必须保持不变。

对应规格：SPEC-ORDER-QUERY-03 / SPEC-ORDER-QUERY-04。
本文件在"代码修改前"就存在，且在修改前后都必须全部通过。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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
]


def test_query_without_filters_returns_everything():
    """不带任何过滤条件时返回全部订单。"""
    result = list_orders(ORDERS)
    assert [o.order_id for o in result] == ["O-1", "O-2", "O-3"]


def test_result_preserves_input_order():
    """SPEC-ORDER-QUERY-03：结果保持入参顺序，不得重新排序。"""
    shuffled = [ORDERS[2], ORDERS[0], ORDERS[1]]
    result = list_orders(shuffled)
    assert [o.order_id for o in result] == ["O-3", "O-1", "O-2"]


def test_empty_input_returns_empty_list():
    """SPEC-ORDER-QUERY-04：空输入返回空列表且不抛异常。"""
    assert list(list_orders([])) == []


def test_query_does_not_mutate_input():
    """查询不得修改调用方传入的序列。"""
    source = list(ORDERS)
    list_orders(source, status="PAID")
    assert [o.order_id for o in source] == ["O-1", "O-2", "O-3"]
