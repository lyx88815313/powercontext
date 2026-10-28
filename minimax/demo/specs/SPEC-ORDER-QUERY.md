# SPEC-ORDER-QUERY 订单列表查询规格

| 项 | 值 |
| --- | --- |
| spec_id | SPEC-ORDER-QUERY |
| version | 1.2.0 |
| owner | fulfillment-team |
| applies_to | `fulfillment.query` |
| status | effective |

## SPEC-ORDER-QUERY-01 订单状态筛选（目标条目）

`fulfillment.query.list_orders` 必须支持按订单状态 `status` 过滤。

- 当 `status` 为 `None` 时**不得**过滤，返回全部订单（必须保持既有行为）。
- 当 `status` 为非空字符串时，只返回 `order.status == status` 的订单（必须）。
- 当 `status` 不属于允许集合 `{PAID, SHIPPED, CANCELLED, REFUNDED}` 时，必须抛出
  `fulfillment.errors.InvalidFilterError`，**不得**静默返回空列表（必须）。

## SPEC-ORDER-QUERY-02 金额下界筛选（目标条目）

- `min_amount` 必须按“大于等于”语义过滤，即保留 `order.amount >= min_amount` 的订单（必须）。
- `min_amount == 0` 必须等价于不过滤（必须保持）。
- `min_amount < 0` 必须抛出 `fulfillment.errors.InvalidFilterError`（必须）。

## SPEC-ORDER-QUERY-03 结果稳定性

筛选结果必须保持入参顺序，**不得**重新排序，**不得**去重（必须保持既有行为）。

## SPEC-ORDER-QUERY-04 空输入

传入空序列时必须返回空序列，**不得**抛出异常（必须保持既有行为）。

## SPEC-ORDER-QUERY-05 参数互不干扰

同时给出 `status` 与 `min_amount` 时，两个条件之间是“与”关系（必须）。
任一条件为 `None` 时只应用另一个条件（必须）。
