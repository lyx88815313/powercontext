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
