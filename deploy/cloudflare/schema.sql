-- 访客记录表：只给自己查（wrangler d1 execute），没有对外的网页界面。
-- site 区分是哪个 Pages 项目命中的（'bot' = daily-digest-bot 全频道，'news' = daily-digest-news 仅新闻），
-- 由 Pages Function 里的 SITE_NAME 环境变量决定，两个项目共用这一个数据库。
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,       -- ISO 8601 UTC 时间戳
    site TEXT NOT NULL,     -- 'bot' | 'news'
    ip TEXT,                -- CF-Connecting-IP
    country TEXT,           -- Cloudflare 解析的国家代码，如 'SE'
    city TEXT,
    path TEXT NOT NULL,     -- 请求的路径
    referer TEXT
);

CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits (ts);
CREATE INDEX IF NOT EXISTS idx_visits_site_ts ON visits (site, ts);
