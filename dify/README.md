# 赛博客栈 · 记仇掌柜（Dify × seekdb）

> 一个会记住每次到访的 AI 客栈掌柜。你说过的每句话都写进 seekdb，下次进门他还记得你上次是夸茶还是砸场子。

- 赛道：Dify 生态赛题「Just for Fun——用 Dify 做一个好玩的智能应用」（20 分）
- 技术组合：**Dify（必选，流程编排）+ seekdb（记忆存储与召回）**
- 提交目录：`dify/`（分支 `2026-competition-ecosystem`）

---

## 1. 作品创意与玩法

「赛博客栈」是一家不讲价、不赊账、只记账的客栈。掌柜记性极好：你第一次进门他盘问你，第二次他记得你上次说了什么，第三次他会拿你的旧话挤对你。

玩家做的事很简单——报个名号，然后随便说话：

| 你说 | 掌柜的反应依据 |
| --- | --- |
| 「掌柜的，来壶热茶，谢谢」 | 情绪分 +1，热络，可能额外送一碟瓜子 |
| 「你这茶也太难吃了，差评」 | 情绪分 −2，记仇，下次开口就翻旧账 |
| 「你上次是怎么说我的？」 | 从 seekdb 召回历史，复述你上次说过的话 |

**好玩的点**：记忆是**跨会话、跨天**的。关掉页面隔天再来，掌柜仍然记得你，甚至给你起了外号（`面生的` / `爱挑刺的` / `熟客` / `贵客` / `砸场子的`）。

**核心功能**：一句话进、有记忆的一句话出。Dify 负责编排，seekdb 负责“记得住”。

## 2. 架构与分工

```
Dify Chatflow
  ┌─────────┐   ┌──────────────────┐   ┌──────────────┐   ┌──────────────────┐   ┌────────┐
  │ 报上名号 │ → │ 召回记忆(HTTP)    │ → │ 掌柜回话(LLM)│ → │ 写入记忆(HTTP)   │ → │ 直接回复│
  │ start   │   │ seekdb 读        │   │ 依据记忆扮演  │   │ seekdb 写        │   │ answer │
  └─────────┘   └──────────────────┘   └──────────────┘   └──────────────────┘   └────────┘
                          ↓                     ↑                    ↓
                   inn_player / inn_memory（seekdb）
```

| 组成 | 承担什么 | 具体证据 |
| --- | --- | --- |
| **Dify** | 串联输入 → 记忆召回 → 模型生成 → 记忆写入 → 输出；DSL 即全部编排 | `dsl/innkeeper-chatflow.yml`（5 节点 4 边） |
| **seekdb** | 应用数据存储与检索：`inn_player` 存情绪/次数/外号，`inn_memory` 存逐轮对话，`LIKE` 支持关键词回查 | `memory_service/schema.sql`、`evidence/seekdb_selftest.log` |
| 记忆服务 | Dify 与 seekdb 之间的 HTTP 适配层（Dify 无 MySQL 节点） | `memory_service/server.py` |

seekdb 在这里**不是** Dify 自身的元数据库，而是**应用的数据存储**：每一轮对话都会真实写入，下一轮由 LLM 读出来影响回答。README 第 6 节给出“本轮没有写库 vs 写了库”的对照实验。

## 3. 运行环境与版本

| 项 | 版本 / 说明 |
| --- | --- |
| Dify | 1.x（DSL 以 `version: 0.1.5` 导出，1.x 可直接导入；0.15 以前版本不支持该 DSL 结构） |
| 模型 | OpenAI 兼容 Chat 模型，默认写的是 `gpt-4o-mini`；也可换 DeepSeek / Qwen 等，见第 5 步 |
| seekdb | 1.4.0（`oceanbase/seekdb` 镜像，MySQL 协议，默认端口 2881） |
| Python | 3.9+（记忆服务只依赖标准库 + `pymysql`） |
| 插件 | 无自定义插件；仅使用 Dify 内置 `HTTP Request` / `LLM` / `Answer` 节点 |

## 4. 从零跑起来（约 10 分钟）

> **没有 Docker 也能跑。** 4.1 + 4.3 可以用一条命令替代（用官方 `pylibseekdb` 在进程内起 seekdb）：
>
> ```bash
> cd memory_service
> pip install pylibseekdb pymysql
> python3 dev_embedded.py        # 嵌入式 seekdb + 记忆服务，监听 0.0.0.0:8000
> ```
>
> 若 Dify 在云端（不在本机），再开一个终端把 8000 暴露到公网：
>
> ```bash
> ./dev_stack.sh                 # 会自动建隧道、打印公网地址，并回填 dsl/innkeeper-chatflow-test.yml
> ```
>
> 然后把 Dify 两个 HTTP 节点改成脚本打印的 `https://xxxx.lhr.life` 地址。这条路只用于本地验证，正式提测仍建议按 4.1–4.3 的 seekdb 部署方式。

### 4.1 启动 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 \
  -v $(pwd)/seekdb-data:/var/lib/oceanbase \
  oceanbase/seekdb:latest
```

> 已用原生/嵌入式 seekdb 1.4.0 验证；`docker logs seekdb` 出现 ready 即可。

### 4.2 初始化记忆库

```bash
mysql -h127.0.0.1 -P2881 -uroot < memory_service/schema.sql
```

会创建库 `inn` 与两张表 `inn_player`、`inn_memory`。

### 4.3 启动记忆服务

```bash
cd memory_service
pip install -r requirements.txt

export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_PASSWORD='' SEEKDB_DB=inn
python3 server.py            # 监听 127.0.0.1:8000
```

自检（不需要 Dify，直接验证 seekdb 读写链路）：

```bash
python3 selftest.py          # 6 项检查，全 PASS 即正常
```

### 4.4 导入 Dify DSL

1. Dify → **工作室 / Studio** → 创建空白应用旁边下拉 → **导入 DSL 文件**
2. 选择 `dsl/innkeeper-chatflow.yml`
3. 打开应用 → 右上角 **发布 / Publish**

### 4.5 绑定模型与记忆服务地址

- **模型**：打开「掌柜回话」LLM 节点，把模型换成你环境里已配置的（如 `deepseek-chat`、`qwen-max`），温度建议 0.7–0.9。
- **记忆服务地址**：两个 HTTP 节点默认写的是 `http://127.0.0.1:8000`。
  - Dify 以 Docker 部署时，容器里的 `127.0.0.1` 指向容器自己，必须改成宿主机地址：`http://host.docker.internal:8000`（macOS/Windows）或宿主机的局域网 IP（Linux）。
  - 改完两个节点后重新 **发布**。

### 4.6 开始玩

应用「赛博客栈·记仇掌柜」→ 聊天窗口 → 填入名号（示例：`阿罗`）→ 开始对话。

应用入口：Dify 应用预览页的聊天窗口；也可以用同一应用的服务 API（`POST /v1/chat-messages`）。

## 5. 外部服务与网络

| 项 | 要求 |
| --- | --- |
| 模型服务 | 需要可在 Dify 中配置的 Chat 模型（API Key 由使用者在 Dify 模型供应商里填写，**本仓库不含任何密钥**） |
| Dify → 记忆服务 | Dify 运行环境需能访问记忆服务的宿主地址；Docker 部署时用 `host.docker.internal` |
| 记忆服务 → seekdb | 默认 TCP 2881；`selftest.py` 也支持 `SEEKDB_SOCKET` 走 unix socket |
| 资源 | seekdb 单机 1–2 GB 内存足够；记忆服务是单进程，内存 < 100 MB |

## 6. 证明 seekdb 真的在起作用（强烈建议按此复验）

同样一句话，**关掉记忆服务**前后对比：

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| 1 | 正常对话 3 轮（夸一句、骂一句、再问一句） | 掌柜会引用前文，语气随情绪变化 |
| 2 | 查库：`SELECT player,mood,turns,alias FROM inn.inn_player;` | 有 1 行，`turns=3`，`mood` 为负数 |
| 3 | 查记忆：`SELECT turn,user_text,npc_text FROM inn.inn_memory WHERE player='阿罗';` | 3 行，逐轮对话原样落库 |
| 4 | `Ctrl+C` 停掉记忆服务，再发一句 | Dify 的 HTTP 节点报错（重试 2 次后失败），说明回答链路真的依赖 seekdb 数据 |
| 5 | 重新启动服务，**新开一个会话**（同一名号） | 掌柜仍提起历史，证明记忆持久化在 seekdb 而非对话上下文 |

第 5 步是关键：Dify 对话窗口清空后仍然“记得”，只能来自 seekdb。

## 6.1 实测记录（Dify 云端版 + 隧道）

2026-11-09 用 Dify 云端版（`https://api.dify.ai/v1`）跑通，完整记录见 `evidence/dify_api_transcript.md`，结论：

| 轮次 | 会话 | 客官 | 掌柜 | seekdb 状态 |
| --- | --- | --- | --- | --- |
| 1 | A | 掌柜的，来壶热茶，谢谢 | 这位客官真会享受，茶水马上就来。请问要加点什么小吃吗？ | `turns=1, mood=1` |
| 2 | A | 你这茶也太难吃了，差评 | 哎呀，上回您就说过这茶不好喝，这次要不要试试换个口味？ | `turns=2, mood=-1, alias=爱挑刺的` |
| 3 | **B（全新会话）** | 掌柜的，再来一壶 | 上回你可是说我的茶难喝，这次还真敢再来呀？ | `turns=3` |

第 3 轮是新会话，Dify 侧没有任何历史消息，掌柜仍能翻旧账 —— 记忆只可能来自 seekdb 的 `inn_memory`。

复现方式：`python3 scripts/api_smoke.py`（需要 `DIFY_KEY`、`DIFY_BASE`、`MEM_BASE` 三个环境变量）。

## 7. 已知限制

1. 记忆检索用 `LIKE` 关键词匹配，没有接向量检索；语义相近但字面不同的旧事可能召回不到（结构化的情绪/次数不受影响）。
2. 情绪分由关键词表 + 规则计算（`server.py` 里 `mood_delta`），不是模型判定；模型只负责“按情绪说话”。
3. 记忆服务是演示级单进程 HTTP 服务：无鉴权、无线程池调优，不要直接暴露到公网。
4. 掌柜回话被限制为「不超过 100 字、单行、不带英文双引号」，是为了让写入节点的 JSON 不被模型输出破坏；代价是表达偏短。
5. Dify 与记忆服务不在同一网络命名空间时，必须把 `127.0.0.1` 改成可路由地址（见 4.5）。
6. 掌柜偶尔会把「这一句」说成「上回」——记忆内容本身来自 seekdb 不会错，但措辞上仍有轻微过度归因，属于小模型的表达习惯；如果要更严格，可在 LLM 节点把规则 1 改写为「引用旧事时必须逐字对应 memories 里的 user 原话」。

## 8. 目录结构

```
dify/
├── README.md                       # 本文件：运行说明
├── dsl/innkeeper-chatflow.yml      # Dify 应用 DSL（导入即用）
├── memory_service/
│   ├── server.py                   # 记忆服务（HTTP → seekdb）
│   ├── schema.sql                  # 建库建表
│   ├── selftest.py                 # 6 项自检，含 seekdb 读写验证
│   ├── dev_embedded.py             # 无 Docker：进程内起 seekdb + 服务
│   ├── dev_stack.sh                # 无 Docker + Dify 云端：一键起栈并开公网隧道
│   └── requirements.txt            # pymysql
├── evidence/seekdb_selftest.log    # 自检实际输出（seekdb 1.4.0）
└── validation_samples.md           # 验证样例与预期表现
```

## 9. 参考

- Dify DSL 导入导出：<https://docs.dify.ai/guides/application-publishing/importing-and-exporting-an-app>
- Dify HTTP Request 节点：<https://docs.dify.ai/guides/workflow/node/http-request>
- seekdb：<https://github.com/oceanbase/seekdb>
- 赛题说明：OceanBase 数据库大赛「Dify 生态赛题」
