# SPEC-PRICING 结算金额规格

| 项 | 值 |
| --- | --- |
| spec_id | SPEC-PRICING |
| version | 1.1.0 |
| owner | fulfillment-team |
| applies_to | `fulfillment.pricing` |
| status | effective |

## SPEC-PRICING-01 金额合法性

订单金额必须 >= 0，否则抛出 `fulfillment.errors.InvalidAmountError`（必须）。

## SPEC-PRICING-02 折扣率合法性

折扣率必须落在 `[0, 1)` 区间内，否则抛出 `fulfillment.errors.InvalidAmountError`（必须）。

## SPEC-PRICING-03 结算公式

折后金额 = 订单金额 * (1 - 折扣率)；税额 = 折后金额 * 6%；结算金额 = 折后金额 + 税额。
每一步都必须按分（两位小数）四舍五入（必须保持）。

> 本规格涉及的模块属于回归保护范围，订单查询改造不得影响其行为。
