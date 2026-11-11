# 上下文增量传输与完整重建

新增 `powercontext.builtin.runtime.context_pack`，提供不可变的 ContextPack、ContextPackDelta、apply_context_pack_delta 与 ContextPackError。`ScopedContextApplication.prepare_incremental` 调用普通 prepare 获取目标正文，并将其编码成完整包或相对于调用方 base_pack 的增量，不修改普通 prepare 的检索、正文、引用或授权行为。

完整包仅包含协议约定的 schema、items、digest、byte_count。分块采用保持原始字节的确定性行边界和有界 UTF-8 切分。摘要针对只含 schema、items 的规范 JSON 计算 SHA-256，byte_count 通过固定点计算包括自身字段的真实序列化长度。

增量包只携带 base_digest、target_digest、公共前缀块数、目标剩余块及 schema。接收端仅凭反序列化后的 base 和 delta 重建，校验 base、前缀范围、UTF-8、Base64、目标摘要和完整包字节数。重复或额外 JSON 字段、无效编码、非法整数和篡改均以 ContextPackError 失败。合法空结果对应空包，也支持由非空上下文增量变为空上下文。

实现不使用发送端缓存，不按 Memory 修订或测量轮次返回预置内容，不替换或删减引用。协议只复用相同的完整前缀块，正文首部、中部、尾部变化和截短均可无损重建。

## 验证

公开验收 14 项通过，补充协议测试及原有上下文回归测试共 109 项通过。包含 UTF-8 边界、独立解码重建、任意合法公共前缀、严格字段与类型、篡改、真实 Runtime 非空和空结果，以及包含自身字段的准确字节数。

另进行了独立本地成本实验：使用固定版本 QMSum ES2004a 原始会议中的文本，初始 8 个 turn，40 轮 prepare，在第 8、16、24、32 轮追加 turn，并切换查询；每轮独立序列化并在接收端重建，与普通 prepare 的正文逐字节核对。一次以 remote、things 为查询的运行产生 8 种非空上下文，结果如下：

| 口径 | 累计字节 |
| --- | ---: |
| 原始完整 UTF-8 正文 | 60,980 |
| 首次完整包及后续增量（含全部协议开销） | 23,845 |
| 每轮完整 v1 pack | 88,800 |
| 各轮独立 gzip(level=9, mtime=0) | 31,756 |

对原始正文的净节省约 60.9%，对完整 v1 pack 约 73.1%，对独立 gzip 约 24.9%。这些是独立本地测量数据，包含该次 Runtime 生成的真实引用，不能视为官方隐藏 40 轮成本序列的结果。固定依赖的 Linux 验收由分支自动工作流运行。

```sh
make -C competition test-incremental
```
