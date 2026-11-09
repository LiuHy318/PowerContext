# 验证样例（按步骤执行即可复现）

> 目标：让评审在不看我本地环境的情况下，5 分钟内复现「记忆写入 → 记忆召回 → 影响回答」的闭环。
> 所有样例都用同一个名号 `阿罗`，每次验证前先执行 **样例 0** 复位。

---

## 样例 0：复位初始状态

```bash
curl -s -X POST http://127.0.0.1:8000/memory/reset \
  -H 'Content-Type: application/json' -d '{"player":"阿罗"}'
```

预期返回：

```json
{"ok": true, "player": "阿罗", "deleted_memories": 3}
```

再查库确认已清空：

```sql
SELECT COUNT(*) FROM inn.inn_player WHERE player='阿罗';    -- 预期 0
SELECT COUNT(*) FROM inn.inn_memory WHERE player='阿罗';    -- 预期 0
```

---

## 样例 1：初次到访（无记忆）

**初始状态**：已复位。

**输入**（Dify 聊天窗口，名号 `阿罗`）：

```
掌柜的，来壶热茶
```

**预期**：掌柜客气但盘问来意，不会提到任何旧事；不会说“上回”之类的话。

**验证**：

```sql
SELECT player, mood, turns, alias FROM inn.inn_player WHERE player='阿罗';
-- 预期 1 行，turns=1，mood>=0，alias 为「面生的」或「熟客」
SELECT * FROM inn.inn_memory WHERE player='阿罗';
-- 预期 1 行，user_text 含「热茶」
```

---

## 样例 2：记仇（情绪分下降 + 下次翻旧账）

**初始状态**：样例 1 之后。

**输入**：

```
你这茶也太难吃了，差评
```

**预期**：掌柜语气变硬，可能加价或记账。

**验证情绪分变化**：

```sql
SELECT mood, turns FROM inn.inn_player WHERE player='阿罗';
-- 预期 mood 比上一轮至少低 2（「难吃」「差评」两个负面词），turns=2
```

**关键验证——记忆被复用**：**新开一个会话**（同一名号 `阿罗`），输入：

```
掌柜的，再来一壶
```

**预期**：掌柜主动提起上一轮的差评（例如“上回你说我的茶难吃”“先把上回的差评收回”），而不是当没事发生。

> 这一步证明记忆存在 seekdb 而非 Dify 的对话上下文：新会话没有历史消息，掌柜只能从 `inn_memory` 读到旧事。

---

## 样例 3：记恩（情绪分回升 + 态度转好）

**初始状态**：样例 2 之后（mood 为负）。

**输入**：

```
上回是我嘴欠，这茶真香，谢谢掌柜
```

**预期**：情绪分回升，掌柜态度缓和，仍会带一句旧账但明显软化。

**验证**：

```sql
SELECT mood, alias FROM inn.inn_player WHERE player='阿罗';
-- 预期 mood 上升（「谢谢」「香」两个正面词），alias 可能从「爱挑刺的」变回「面生的」/「熟客」
```

---

## 样例 4：关键词回查历史（seekdb LIKE 检索）

```bash
curl -s "http://127.0.0.1:8000/memory/search?player=%E9%98%BF%E7%BD%97&q=%E8%8C%B6"
```

预期返回该玩家所有提到「茶」的轮次：

```json
{"player":"阿罗","q":"茶","hits":[{"turn":1,"user_text":"掌柜的，来壶热茶","npc_text":"...","mood_after":1}, ...]}
```

对应 SQL：

```sql
SELECT turn, user_text FROM inn.inn_memory
 WHERE player='阿罗' AND (user_text LIKE '%茶%' OR npc_text LIKE '%茶%') ORDER BY turn;
```

---

## 样例 5：异常路径（工具失败时不编造）

**操作**：`Ctrl+C` 停掉记忆服务，然后在 Dify 聊天窗口发任意一句。

**预期**：Dify 的「召回记忆（seekdb）」节点重试 2 次后失败，工作流在该节点中断；应用不会返回一段看似正常、实则没有记忆依据的回答。

**恢复**：重启 `python3 server.py`，重新发布应用（或直接重试对话）即可继续。

---

## 附：一键复现（不依赖 Dify）

对着 seekdb 直接验证记忆服务（会自动建表、起服务、跑 6 项检查）：

```bash
cd memory_service
python3 selftest.py
```

实际输出见 `evidence/seekdb_selftest.log`：

```
[PASS] seekdb 连通  5.7.25-OceanBase seekdb-v1.4.0.0
[PASS] 新玩家召回为空记忆
[PASS] 跨请求召回 2 轮记忆
[PASS] 情绪分按用词下降  mood=-1 alias=爱挑刺的
[PASS] 记忆摘要含最近往来
[PASS] 关键词检索命中历史
自检结果：全部通过
```
