"""业务异常类型。"""

from __future__ import annotations


class FulfillmentError(Exception):
    """业务根异常。"""


class InvalidFilterError(FulfillmentError):
    """筛选条件非法（未知状态、负数金额下界等）。"""


class InvalidAmountError(FulfillmentError):
    """金额非法（负数金额、非法折扣率）。"""
