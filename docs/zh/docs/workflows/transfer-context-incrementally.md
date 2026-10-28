---
title: 增量传输上下文
description: 把准备好的上下文作为 Context Pack 发送，并基于上一轮的数据包只发送变化的后缀。
---

# 增量传输上下文

普通 `prepare` 每次都会返回完整的准备结果。当调用方已经持有上一轮结果时，其中大部分字节是重复的。Context Pack
协议先发送一次完整数据包，之后每轮只发送一个增量包：增量包说明上一轮数据包中哪一段前缀可以保留，并携带新的后缀。
接收端仅凭序列化数据即可重建本轮上下文，不需要访问发送端的进程。

## 先准备数据包，再准备增量包

`ScopedContextApplication.prepare_incremental` 接受与 `prepare` 相同的请求，返回的正文也完全相同。没有提供
基准数据包时返回完整数据包；提供了基准数据包时返回把该基准数据包变成本轮数据包的增量包。

```python
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    apply_context_pack_delta,
)

request = PrepareContextRequest(query="remote control", max_bytes=8000)

first = await context.prepare_incremental(request)
assert isinstance(first, ContextPack)

second = await context.prepare_incremental(request, base_pack=first)
assert isinstance(second, ContextPackDelta)
```

接收端保留已经持有的数据包，并逐个应用增量包：

```python
held: ContextPack = first
for delta in deltas:
    held = apply_context_pack_delta(held, delta)
    text = b"".join(held.items).decode("utf-8")
```

`apply_context_pack_delta` 在返回重建结果之前会校验基准摘要、前缀范围和目标摘要，被截断或不匹配的增量包会抛出
`ContextPackError`，而不会悄悄产出另一份上下文。

## 读取线上格式

两种对象都是规范化 JSON：UTF-8 编码、键排序、分隔符为 `,` 和 `:`，不含多余空白。

| 对象 | 字段 |
| --- | --- |
| 数据包 | `schema`、`items`、`digest`、`byte_count`；`schema` 为 `powercontext.context-pack.v1` |
| 增量包 | `schema`、`base_digest`、`target_digest`、`prefix_items`、`appended`；`schema` 为 `powercontext.context-pack-delta.v1` |

`items` 与 `appended` 在线上是标准 Base64 字符串数组，在内存中是 `bytes` 元组。数据包摘要是仅含 `schema` 与
`items` 的规范化 JSON 的 SHA-256，写成 `sha256:<hex>`。数据包的 `byte_count` 等于其自身序列化后的长度，包含该字段
本身。增量包通过 `byte_count` 属性报告自身大小，该属性不写入线上 JSON。

`ContextPack.from_content` 以确定性方式切分文本。每块都是有效 UTF-8，且不超过调用方指定的字节上限，默认 512
字节；按顺序拼接可以逐字节还原原文。字符本身超过上限时抛出 `ContextPackError`。prepare 的空结果对应空数据包，
其增量包重建后为空字节。

## 测量节省

`scripts/measure_context_pack.py` 用真实 Runtime 驱动重复的 prepare 序列，并把线上字节数与普通上下文以及两项
对照一起报告：

```bash
python scripts/measure_context_pack.py --meeting data/ALL/test/ES2004a.json
python scripts/measure_context_pack.py --meeting data/ALL/test/ES2004a.json --append-every 4
```

报告包含 `raw_full_bytes`（每轮普通 prepare 正文的 UTF-8 字节数）、`incremental_bytes`（首次完整包与每个增量包
的序列化字节数）、完整 v1 数据包的累计字节数，以及对相同正文逐轮 `gzip.compress(compresslevel=9, mtime=0)` 的累计
字节数。节省率为负表示增量协议比该对照更大。gzip 对照每轮单独压缩、不跨请求复用状态，因此只是观测项而非目标。

节省幅度取决于上下文有多少重复：首个包总是携带完整正文，之后每个增量包只携带公共前缀之后的后缀。上下文完全重复的
轮次只需付出两段摘要的代价，而正文开头就发生变化的轮次，代价接近重新发送一个完整包。

## 在真实会议转录上的测量

以检索词 `remote` 检索 QMSum 会议 `ES2004a`，40 轮可以得到三组参考数据。取用的 turn 是正文字节数在 120 到 900
之间且包含该检索词的会议 turn，按原始顺序排列。

| 序列 | 不同上下文数 | 普通正文字节 | 增量协议字节 | 完整 v1 数据包字节 | gzip 字节 | 相对普通正文节省 |
| --- | --- | --- | --- | --- | --- | --- |
| 每轮同一个查询 | 1 | 105,760 | 13,689 | 148,200 | 46,760 | 87.06% |
| 每四轮追加一个 turn | 10 | 142,636 | 54,553 | 197,692 | 47,884 | 61.75% |
| 每轮换一个查询 | 4 | 61,660 | 85,654 | 88,980 | 29,080 | -38.91% |

第一行是最好情况，最后一行是最坏情况：一旦选中的证据从第一个字节就发生变化，增量包就得重新携带整段正文，而仅
Base64 编码这一项就比普通正文多出约三分之一。对两项对照观测，同三个序列的节省率分别为 90.76%、72.41% 和
-194.55%；最后一个负值如实反映了前缀复用协议在几乎没有重复内容的序列上的代价。
