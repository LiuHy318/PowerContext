# HTTP 重排服务与跨批次全局排序

项目五的实现为 `powercontext.builtin.artifacts.memory.http_reranker.HTTPMemoryReranker`，可通过 Runtime 的 `memory_reranker` 注入。实现遵循已有 MemoryReranker 协议，不改写候选正文、Memory 引用、entry ID 或 entry version ID。

构造器接收 endpoint，以及调用方创建的 `httpx.AsyncClient`、正整数 batch_size 和 policy_id。客户端的生命周期、认证与超时由调用方管理，重排器不会关闭客户端。评测提供固定的 httpx 依赖。

每批最多发送 batch_size 个候选，rank 始终使用完整候选序列中从 1 开始的全局编号。所有批次评分完成后才统一选取前 limit 个结果，按分数降序排列，同分按原 rank 升序排列。返回的 InferenceUsage.requests 为实际发起的批次数，不做隐式重试或重定向。

响应必须恰好包含 scores 数组，数组中的对象必须恰好包含 rank 和 score。本批的 rank 必须完整覆盖一次；重复、缺失、越界以及其他批次的 rank 均拒绝。布尔值、字符串和浮点 rank 不会隐式转换成整数，score 仅接受有限 JSON 数值。重复 JSON 对象字段、非法 JSON 和其他非法响应抛出 ValueError。HTTP 非 2xx 和连接、超时错误保留 httpx 异常。

本地验证包括 10 项公开验收、原有重排测试以及额外的协议和边界测试。额外测试使用 173 个合成候选、多个批次大小与乱序响应，核对全局排序、稳定同分、原文保留和请求计数；另通过真正的本地 HTTP 服务验证传输。真实 Memory 集成由公开验收覆盖。

运行官方公开验收：

```sh
make -C competition test-rerank
```

分支自动验收同时运行 PDF 与 HTTP 重排测试。公开验收不包括完整的隐藏 SciFact 数据集，因此不将协议测试通过等同于已测得隐藏集 Recall@10、MRR@10 或官方得分。
