-- 赛博客栈 · seekdb 记忆库初始化
-- 用法：mysql -h127.0.0.1 -P2881 -uroot < schema.sql
--      或 pylibseekdb 内嵌实例里按顺序执行。

CREATE DATABASE IF NOT EXISTS inn DEFAULT CHARACTER SET utf8mb4;
USE inn;

-- 玩家状态：情绪分、来店次数、掌柜私下给的外号
CREATE TABLE IF NOT EXISTS inn_player (
  player     VARCHAR(64)  NOT NULL COMMENT '玩家标识，Dify 侧传入',
  mood       INT          NOT NULL DEFAULT 0 COMMENT '情绪分，负数记仇、正数记恩',
  turns      INT          NOT NULL DEFAULT 0 COMMENT '累计来店轮次',
  alias      VARCHAR(64)  NULL COMMENT '掌柜私下起的外号',
  updated_at DATETIME     NOT NULL COMMENT '最近互动时间',
  PRIMARY KEY (player),
  KEY idx_mood (mood)
) ORGANIZATION HEAP;

-- 逐轮记忆：每一句玩家输入与掌柜回话都落库，供后续召回
CREATE TABLE IF NOT EXISTS inn_memory (
  player      VARCHAR(64)   NOT NULL,
  turn        INT           NOT NULL COMMENT '该玩家的第几轮',
  user_text   VARCHAR(1000) NOT NULL COMMENT '玩家原话',
  npc_text    VARCHAR(2000) NOT NULL COMMENT '掌柜回话',
  mood_after  INT           NOT NULL COMMENT '本轮结束后的情绪分',
  created_at  DATETIME      NOT NULL,
  PRIMARY KEY (player, turn),
  KEY idx_created (created_at)
) ORGANIZATION HEAP;
