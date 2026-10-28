# 开发上下文：TASK-001 订单列表查询的状态筛选与金额下界筛选

## 1. 本次任务

在 fulfillment-svc 中实现 `fulfillment.query.list_orders` 的订单状态筛选与金额下界筛选， 使其符合 SPEC-ORDER-QUERY 的要求：status 为 None 时不过滤、非空时只返回状态匹配的订单、 非法 status 与负数 min_amount 必须抛出 InvalidFilterError；min_amount 按“大于等于”语义过滤、 min_amount == 0 等价于不过滤。同时必须保持 SPEC-ORDER-QUERY-03（顺序稳定）与 SPEC-ORDER-QUERY-04（空输入返回空列表）等既有行为不变，且不得影响结算模块行为。


- 上下文摘要（digest）：`0fd0c9d3d5b0341b6b2d7fdf740decebb00713fcfee69390a7ae3ea62d0e7206`
- 数据库：5.7.25-OceanBase seekdb-v1.4.0.0 / 库 `sdd_context`
- 检索方式：spec_item=fulltext, code_unit=fulltext, test_unit=fulltext

## 2. 适用的规格条目

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-01 订单状态筛选（目标条目）

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-01`
- 版本：`1.3.0`　内容哈希：`31ddb36bfd81ba69`

`fulfillment.query.list_orders` 必须支持按订单状态 `status` 过滤。

- 当 `status` 为 `None` 时**不得**过滤，返回全部订单（必须保持既有行为）。
- 当 `status` 为非空字符串时，只返回 `order.status == status` 的订单（必须）。
- 当 `status` 不属于允许集合 `{PAID, SHIPPED, CANCELLED, REFUNDED}` 时，必须抛出
  `fulfillment.errors.InvalidFilterError`，**不得**静默返回空列表（必须）。

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-02 金额下界筛选（目标条目）

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-02`
- 版本：`1.3.0`　内容哈希：`caa8c00e8ded820f`

- `min_amount` 必须按“大于等于”语义过滤，即保留 `order.amount >= min_amount` 的订单（必须）。
- `min_amount == 0` 必须等价于不过滤（必须保持）。
- `min_amount < 0` 必须抛出 `fulfillment.errors.InvalidFilterError`（必须）。

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-03 结果稳定性

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-03`
- 版本：`1.3.0`　内容哈希：`9f088024ab5ef354`

筛选结果必须保持入参顺序，**不得**重新排序，**不得**去重（必须保持既有行为）。

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-04 空输入

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-04`
- 版本：`1.3.0`　内容哈希：`be1accac41ce175b`

传入空序列时必须返回空序列，**不得**抛出异常（必须保持既有行为）。

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-05 参数互不干扰

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-05`
- 版本：`1.3.0`　内容哈希：`4ed4b9b6263f739a`

同时给出 `status` 与 `min_amount` 时，两个条件之间是“与”关系（必须）。
任一条件为 `None` 时只应用另一个条件（必须）。

### SPEC-PRICING / SPEC-PRICING-03 结算公式

- 来源：`specs/SPEC-PRICING.md`
- 规格编号：`SPEC-PRICING` / 条目 `SPEC-PRICING-03`
- 版本：`1.1.0`　内容哈希：`1c52e04b83cfbec1`

折后金额 = 订单金额 * (1 - 折扣率)；税额 = 折后金额 * 6%；结算金额 = 折后金额 + 税额。
每一步都必须按分（两位小数）四舍五入（必须保持）。

> 本规格涉及的模块属于回归保护范围，订单查询改造不得影响其行为。

### SPEC-ORDER-QUERY / SPEC-ORDER-QUERY-06 状态集合来源

- 来源：`specs/SPEC-ORDER-QUERY.md`
- 规格编号：`SPEC-ORDER-QUERY` / 条目 `SPEC-ORDER-QUERY-06`
- 版本：`1.3.0`　内容哈希：`ec98a9933875001d`

允许的订单状态集合必须与本规格条目 SPEC-ORDER-QUERY-01 中列出的集合保持一致，
**不得**在代码里另行硬编码一份不同的集合（必须）。

### SPEC-PRICING / SPEC-PRICING-01 金额合法性

- 来源：`specs/SPEC-PRICING.md`
- 规格编号：`SPEC-PRICING` / 条目 `SPEC-PRICING-01`
- 版本：`1.1.0`　内容哈希：`d19350076e202099`

订单金额必须 >= 0，否则抛出 `fulfillment.errors.InvalidAmountError`（必须）。

### SPEC-PRICING / SPEC-PRICING-02 折扣率合法性

- 来源：`specs/SPEC-PRICING.md`
- 规格编号：`SPEC-PRICING` / 条目 `SPEC-PRICING-02`
- 版本：`1.1.0`　内容哈希：`6699b10689ba3489`

折扣率必须落在 `[0, 1)` 区间内，否则抛出 `fulfillment.errors.InvalidAmountError`（必须）。

## 3. 需要修改的代码

### `src/fulfillment/query.py`

- 来源单元：`src/fulfillment/query.py`
- 涉及符号：`list_orders`

```python
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
```

### `src/fulfillment/errors.py`

- 来源单元：`src/fulfillment/errors.py`
- 涉及符号：`InvalidFilterError`, `FulfillmentError`, `InvalidAmountError`

```python
"""业务异常类型。"""

from __future__ import annotations


class FulfillmentError(Exception):
    """业务根异常。"""


class InvalidFilterError(FulfillmentError):
    """筛选条件非法（未知状态、负数金额下界等）。"""


class InvalidAmountError(FulfillmentError):
    """金额非法（负数金额、非法折扣率）。"""
```

### `src/fulfillment/pricing.py`

- 来源单元：`src/fulfillment/pricing.py`
- 涉及符号：`settlement_amount`

```python
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
```

### `src/fulfillment/__init__.py`

- 来源单元：`src/fulfillment/__init__.py`

```python
"""订单履约服务（fulfillment-svc）业务包。

这是一个用于竞赛演示的最小可运行业务工程：纯标准库实现 + pytest 测试。
"""

from .errors import FulfillmentError, InvalidFilterError
from .models import Order

__all__ = ["FulfillmentError", "InvalidFilterError", "Order"]
```

### `src/fulfillment/models.py`

- 来源单元：`src/fulfillment/models.py`
- 涉及符号：`Order`

```python
"""订单领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Order:
    """一条订单。

    Attributes:
        order_id: 订单号，全局唯一。
        customer_id: 客户号。
        status: 订单状态，取值见 ``fulfillment.query.ALLOWED_STATUSES``。
        amount: 订单金额，单位为元，必须 >= 0。
        created_at: 下单日期。
    """

    order_id: str
    customer_id: str
    status: str
    amount: Decimal
    created_at: date
```

## 4. 用于验证功能及兼容性的测试

### `tests/test_query_target.py`

- 来源单元：`tests/test_query_target.py`
- 用例：`test_status_filter_returns_only_matching_orders`, `test_status_filter_rejects_unknown_status`, `test_min_amount_filter_is_inclusive`, `test_min_amount_negative_is_rejected`, `test_combined_filters_are_intersection`, `test_min_amount_zero_is_equivalent_to_no_filter`, `test_status_filter_none_returns_all_orders`

```python
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
```

### `tests/test_query_regression.py`

- 来源单元：`tests/test_query_regression.py`
- 用例：`test_query_does_not_mutate_input`, `test_result_preserves_input_order`, `test_empty_input_returns_empty_list`, `test_query_without_filters_returns_everything`

```python
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
```

### `tests/test_pricing.py`

- 来源单元：`tests/test_pricing.py`

```python
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
```

## 5. 任务状态（来自 PowerContext）

_（当前无已保存的任务状态，即空状态）_
