-- 一次性迁移：给已经在跑的 daily-digest-visits 数据库补上反爬拦截需要的两列。
-- schema.sql 的 CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，加字段必须用 ALTER TABLE。
-- 跑法：wrangler d1 execute daily-digest-visits --remote --file=deploy/cloudflare/migrate_0002_bot_protection.sql
-- 跑过一次以后这个文件不需要再跑，留着是记录"这个数据库经历过哪些结构变化"。
ALTER TABLE visits ADD COLUMN user_agent TEXT;
ALTER TABLE visits ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_visits_blocked ON visits (blocked);
