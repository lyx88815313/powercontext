---
task_id: TASK-001
title: 订单列表查询的状态筛选与金额下界筛选
objective: >
  在 fulfillment-svc 中实现 `fulfillment.query.list_orders` 的订单状态筛选与金额下界筛选，
  使其符合 SPEC-ORDER-QUERY 的要求：status 为 None 时不过滤、非空时只返回状态匹配的订单、
  非法 status 与负数 min_amount 必须抛出 InvalidFilterError；min_amount 按“大于等于”语义过滤、
  min_amount == 0 等价于不过滤。同时必须保持 SPEC-ORDER-QUERY-03（顺序稳定）与
  SPEC-ORDER-QUERY-04（空输入返回空列表）等既有行为不变，且不得影响结算模块行为。
required:
  spec_items:
    - SPEC-ORDER-QUERY-01
    - SPEC-ORDER-QUERY-02
    - SPEC-ORDER-QUERY-03
    - SPEC-ORDER-QUERY-04
    - SPEC-ORDER-QUERY-05
  code_units:
    - src/fulfillment/query.py::list_orders
    - src/fulfillment/errors.py
  test_units:
    - tests/test_query_target.py::test_status_filter_returns_only_matching_orders
    - tests/test_query_target.py::test_status_filter_rejects_unknown_status
    - tests/test_query_target.py::test_min_amount_filter_is_inclusive
    - tests/test_query_target.py::test_min_amount_negative_is_rejected
    - tests/test_query_target.py::test_combined_filters_are_intersection
    - tests/test_query_regression.py
    - tests/test_pricing.py
---

# TASK-001 订单列表查询的状态筛选与金额下界筛选

## 要改什么

`fulfillment.query.list_orders` 目前只做透传（忽略 `status` 与 `min_amount`）。
需要按 SPEC-ORDER-QUERY-01 / 02 / 05 实现两种筛选，并按 SPEC-ORDER-QUERY-01 / 02
对非法入参抛出 `InvalidFilterError`。

## 哪些原有行为必须保持

- SPEC-ORDER-QUERY-03：筛选结果保持入参顺序，不得重新排序、不得去重。
- SPEC-ORDER-QUERY-04：空输入返回空列表且不抛异常。
- 不带过滤条件时返回全部订单。
- 查询不得修改调用方传入的序列。
- SPEC-PRICING-01/02/03：结算模块行为完全不受影响。

## 验证方式

```bash
cd minimax/demo
python -m pytest tests -v
```

- 目标功能测试：`tests/test_query_target.py`（修改前必须失败，修改后必须全部通过）
- 回归测试：`tests/test_query_regression.py`、`tests/test_pricing.py`（修改前后都必须通过）
