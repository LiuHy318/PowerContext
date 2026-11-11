# OceanBase / seekdb 实测记录（parser 选型依据）

环境：WSL 内 observer `--base-dir=/root/obd1 --port 2881 --parameter memory_limit=4G`，
客户端 `mysql -h127.0.0.1 -P2881 -uroot@sys`（无密码），以及 Python `PyMySQL 1.2.3`。

## 1. 版本

```sql
select version();
-- 5.7.25-OceanBase seekdb-v1.4.0.0
select @@version_comment;
-- OceanBase 4.3.5.3 seekdb (r1.4.0.0) (Built 1-5bce9e06d1281347ad61344e4b01fab9158c58a2 Oct 27 2026)
```

结论：本机就是 **seekdb**（OceanBase 4.3.5.3 内核），满足题目「seekdb 或 OceanBase」的二选一。

## 2. 默认 `space` parser 对中文不可用

```sql
create table ft_probe(
  id bigint primary key auto_increment,
  doc_id varchar(64) not null,
  body text not null,
  fulltext idx_body(body)
);
-- 实际落库 DDL（show create table）：
-- FULLTEXT KEY `idx_body` (`body`) WITH PARSER space PARSER_PROPERTIES=(min_token_size=3,max_token_size=84)

insert into ft_probe(doc_id,body) values
 ('d1','阅读清单服务 新增 按状态 与 最低评分 筛选 保留 既有 排序 分页 行为'),
 ('d3','新增筛选功能 spec requirement filter by status and min rating');

select id,doc_id from ft_probe where match(body) against('筛选' in natural language mode);
-- 空结果（0 行）
select id,doc_id from ft_probe where match(body) against('筛选' in boolean mode);
-- 空结果（0 行）
select id,doc_id,match(body) against('pagination' in natural language mode) from ft_probe
 where match(body) against('pagination' in natural language mode);
-- 2  d2  0.5563447387550394   ← 英文能命中
```

结论：`space` parser 只按空格切词，中文查询完全召回不到；不能用于本项目（规格是中文写的）。
`LIKE '%筛选%'` 能查到（退路可用）。

## 3. `ngram` parser 中英文都能命中

```sql
create table ft_ng(
  id bigint primary key auto_increment,
  body text,
  fulltext idx_b(body) with parser ngram
);
-- DDL：FULLTEXT KEY `idx_b` (`body`) WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2)

insert into ft_ng(body) values ('阅读清单服务新增按状态与最低评分筛选保留既有排序分页行为');
select id, match(body) against('筛选' in natural language mode) s from ft_ng
 where match(body) against('筛选' in natural language mode);
-- 1  0.14745308310991956      ← 中文命中且有分数
```

结论：选 `WITH PARSER ngram (ngram_token_size=2)` 作为 `mm_doc` 的全文索引。

## 4. 其他探测

```sql
create table vp(id bigint primary key auto_increment, v vector(4));   -- 成功，支持 VECTOR 类型
create table jp(id bigint primary key auto_increment, j json);        -- 成功
select * from jp;                                                     -- {"a": 1}
```

`VECTOR` 可用意味着后续可以加向量召回通道（本轮未启用，见 README「已知限制」）。

## 5. 生产查询形态（`ctxsdd plan` 实际执行）

`mm_doc` 表：

```sql
CREATE TABLE IF NOT EXISTS mm_doc (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    doc_id VARCHAR(191) NOT NULL,
    kind VARCHAR(16) NOT NULL,          -- spec | code | test
    path VARCHAR(512) NOT NULL,
    symbol VARCHAR(255) NOT NULL DEFAULT '',
    heading VARCHAR(255) NOT NULL DEFAULT '',
    line_from INT NOT NULL DEFAULT 0,
    line_to INT NOT NULL DEFAULT 0,
    content MEDIUMTEXT NOT NULL,
    content_sha CHAR(64) NOT NULL,
    ingested_at VARCHAR(40) NOT NULL,
    UNIQUE KEY uk_doc_id (doc_id),
    FULLTEXT KEY ft_content (content) WITH PARSER ngram
) DEFAULT CHARSET = utf8mb4;
```

两条检索 SQL（`terms` 为任务关键词，`kinds` 为 `spec|code|test`）：

```sql
-- ① 相关性打分
SELECT doc_id, kind, path, symbol, heading, line_from, line_to, content,
       MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS ft_score
FROM mm_doc
WHERE kind IN (%s, %s, %s)
  AND MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE)
ORDER BY ft_score DESC, doc_id ASC
LIMIT %s;

-- ② 词面兜底（ngram 会切碎 min_rating 这类标识符）
SELECT doc_id, kind, path, symbol, heading, line_from, line_to, content,
       ((content LIKE %s) + (content LIKE %s) + ... ) AS like_hits
FROM mm_doc
WHERE kind IN (%s, %s, %s) AND ((content LIKE %s) OR ...)
ORDER BY like_hits DESC, doc_id ASC
LIMIT %s;
```

合并打分：`score = ft_score + 0.05 * like_hits`，按 `score` 降序、`doc_id` 升序去重取前 k。
实测（`kind='spec'`，12 个关键词）：`fulltext_rows=17`、`like_rows=16`、`elapsed_ms≈529`（首次，含连接），
返回前 6 条规格条目，分数 5.76 → 2.99，`doc_id` 形如 `spec:specs/SPEC-001-list-filter.md#2`。

完整 SQL、参数、命中数、耗时与返回列表落在 `records/ob_query_log.jsonl`（已归档到
`docs/runs/ob_query_log.jsonl`）。
