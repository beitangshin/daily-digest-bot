-- 访客记录表：只给自己查（wrangler d1 execute），没有对外的网页界面。
-- site 区分是哪个 Pages 项目命中的（'bot' = daily-digest-bot 全频道，'news' = daily-digest-news 仅新闻），
-- 由 Pages Function 里的 SITE_NAME 环境变量决定，两个项目共用这一个数据库。
-- user_agent / blocked 是后加的字段（见 migrate_0002_bot_protection.sql，已有数据库要跑
-- 那个文件的 ALTER TABLE 才能补上；这里的 CREATE TABLE 只对全新建库生效）——用来支持
-- User-Agent 反爬拦截：blocked=1 表示这条请求被 _middleware.js 判定为机器人并返回了 403，
-- 没有真的放行到静态资源；blocked=0 才是正常放行的访问。两种都记进同一张表，方便在 /admin
-- 里对比"总请求 vs 被拦截的机器人请求"这个比例。
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,       -- ISO 8601 UTC 时间戳
    site TEXT NOT NULL,     -- 'bot' | 'news'
    ip TEXT,                -- CF-Connecting-IP
    country TEXT,           -- Cloudflare 解析的国家代码，如 'SE'
    city TEXT,
    path TEXT NOT NULL,     -- 请求的路径
    referer TEXT,
    user_agent TEXT,
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits (ts);
CREATE INDEX IF NOT EXISTS idx_visits_site_ts ON visits (site, ts);
CREATE INDEX IF NOT EXISTS idx_visits_blocked ON visits (blocked);
