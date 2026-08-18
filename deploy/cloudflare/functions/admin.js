// /admin -- Basic Auth 保护的访客统计面板，两个 Pages 项目（daily-digest-bot /
// daily-digest-news）各自都挂了这份同样的代码，因为它们共用同一个 D1（daily-digest-visits，
// 用 visits.site 区分是哪个站点），所以不管从哪个域名的 /admin 进来看到的都是两边合并的
// 全量统计，不是各自站点单独的一份。
//
// 账号密码是 Cloudflare Pages 的加密 secret（ADMIN_USER / ADMIN_PASSWORD），不在代码里，
// 不会出现在 git 历史或页面源码里 -- 这个仓库是公开仓库，密码写死在源码里等于公开密码。
// 两个项目（daily-digest-bot / daily-digest-news）各设一次：
//   wrangler pages secret put ADMIN_USER --project-name=<项目名>
//   wrangler pages secret put ADMIN_PASSWORD --project-name=<项目名>
//
// 踩过的坑：在 Windows PowerShell 里用 `"admin" | wrangler pages secret put ...` 这种
// 管道方式喂值，实测存进去的值前面会多一个 UTF-8 BOM 字符（﻿），导致密码怎么填都验证
// 不过 -- PowerShell 管道 / .NET StreamWriter 往子进程 stdin 写文本时默认用带 BOM 的
// UTF-8。验证方式是在这份代码里临时加一段回显 `env.ADMIN_USER` 各字符 charCode 的调试分支，
// 发现第一个字符 code 是 65279（=0xFEFF）。跑得通的办法是绕开 PowerShell 的文本编码：
// 先把值写成一个不带 BOM 的纯 ASCII 文件，再用 `cmd /c "wrangler ... < file"` 做真正的
// 操作系统级 stdin 重定向（不经过 PowerShell/.NET 的字符串编码层）。改密码时如果又验证不过，
// 先怀疑这个，不要怀疑 checkAuth 的逻辑。
//
// 每次打开这个页面都是对 D1 的实时查询，不是定时生成的静态快照，所以"数据是不是最新的"
// 不需要额外操心。

function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const aBytes = enc.encode(a);
  const bBytes = enc.encode(b);
  const len = Math.max(aBytes.length, bBytes.length);
  let diff = aBytes.length ^ bBytes.length;
  for (let i = 0; i < len; i++) {
    diff |= (aBytes[i] || 0) ^ (bBytes[i] || 0);
  }
  return diff === 0;
}

function unauthorized() {
  return new Response("Unauthorized", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="daily-digest-bot admin", charset="UTF-8"' },
  });
}

function checkAuth(request, env) {
  const expectedUser = env.ADMIN_USER;
  const expectedPass = env.ADMIN_PASSWORD;
  if (!expectedUser || !expectedPass) return false; // 没配 secret 就一律拒绝，不给默认放行

  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Basic ")) return false;

  let decoded;
  try {
    decoded = atob(header.slice(6));
  } catch {
    return false;
  }
  const idx = decoded.indexOf(":");
  if (idx === -1) return false;
  const user = decoded.slice(0, idx);
  const pass = decoded.slice(idx + 1);
  return timingSafeEqual(user, expectedUser) && timingSafeEqual(pass, expectedPass);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderTable(headers, rows) {
  if (!rows.length) return "<p class='empty'>暂无数据</p>";
  const head = "<tr>" + headers.map((h) => `<th>${esc(h)}</th>`).join("") + "</tr>";
  const body = rows
    .map((r) => "<tr>" + r.map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>")
    .join("");
  return `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

export async function onRequest(context) {
  const { request, env } = context;

  if (!checkAuth(request, env)) return unauthorized();

  // 下面这批统计（总访问量/独立 IP/地区/热门页面/每日趋势）都只算 blocked=0 的行 --
  // 也就是真正放行、被当成正常访问的请求，不包含被 _middleware.js 判定成机器人拦下来的
  // 那些。旧数据（加反爬拦截之前记的）没有机器人判定，blocked 全是默认值 0，会照常算进
  // "真实流量"里，这个是历史数据的固有局限，不是 bug。
  const db = env.VISITS_DB;
  const [totals, uniqueIps, byCountry, byDay, topPaths, recent, rangeRow, blockedTotal, topBlockedUa, topBlockedIp] =
    await db.batch([
      db.prepare("SELECT site, count(*) AS n FROM visits WHERE blocked = 0 GROUP BY site ORDER BY n DESC"),
      db.prepare("SELECT site, count(DISTINCT ip) AS n FROM visits WHERE blocked = 0 GROUP BY site ORDER BY n DESC"),
      db.prepare("SELECT country, count(*) AS n FROM visits WHERE blocked = 0 GROUP BY country ORDER BY n DESC LIMIT 15"),
      db.prepare(
        "SELECT substr(ts, 1, 10) AS day, site, count(*) AS n FROM visits WHERE blocked = 0 GROUP BY day, site ORDER BY day DESC LIMIT 60"
      ),
      db.prepare(
        "SELECT site, path, count(*) AS n FROM visits WHERE blocked = 0 GROUP BY site, path ORDER BY n DESC LIMIT 20"
      ),
      db.prepare(
        "SELECT ts, site, ip, country, city, path, referer, blocked FROM visits ORDER BY id DESC LIMIT 100"
      ),
      db.prepare("SELECT count(*) AS total, min(ts) AS oldest, max(ts) AS newest FROM visits"),
      db.prepare("SELECT count(*) AS n FROM visits WHERE blocked = 1"),
      db.prepare(
        "SELECT user_agent, count(*) AS n FROM visits WHERE blocked = 1 GROUP BY user_agent ORDER BY n DESC LIMIT 15"
      ),
      db.prepare("SELECT ip, count(*) AS n FROM visits WHERE blocked = 1 GROUP BY ip ORDER BY n DESC LIMIT 15"),
    ]);

  const range = rangeRow.results[0] || { total: 0, oldest: null, newest: null };
  const blockedCount = blockedTotal.results[0]?.n || 0;
  const allowedCount = range.total - blockedCount;

  const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>访客统计 · daily-digest-bot</title>
<meta name="robots" content="noindex, nofollow">
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#6b7280; --border:#e5e7eb; --accent:#2563eb; --card:#f8fafc; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111417; --fg:#e6e6e6; --muted:#9aa4af; --border:#2a2f36; --accent:#6ea8fe; --card:#1a1e23; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; padding:24px; }
  main { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 28px; }
  section.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  section.card h2 { font-size: 14px; margin: 0 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px; }
  th { color: var(--muted); font-weight: 600; }
  .empty { color: var(--muted); font-style: italic; font-size: 13px; }
  .wide { grid-column: 1 / -1; }
  .scroll { max-height: 420px; overflow: auto; }
</style>
</head><body>
<main>
  <h1>📊 访客统计</h1>
  <div class="meta">共 ${range.total} 条记录（含被拦截的） · ${range.oldest ? esc(range.oldest) : "-"} ~ ${range.newest ? esc(range.newest) : "-"}（保留最近 30 天）</div>

  <div class="grid">
    <section class="card">
      <h2>反爬拦截情况</h2>
      ${renderTable(
        ["", "次数"],
        [
          ["✅ 放行（真实流量）", allowedCount],
          ["🚫 拦截（机器人/脚本）", blockedCount],
        ]
      )}
    </section>
    <section class="card">
      <h2>被拦截的 User-Agent（Top 15）</h2>
      <div class="scroll">${renderTable(["User-Agent", "次数"], topBlockedUa.results.map((r) => [r.user_agent || "(空)", r.n]))}</div>
    </section>
    <section class="card">
      <h2>被拦截的 IP（Top 15）</h2>
      <div class="scroll">${renderTable(["IP", "次数"], topBlockedIp.results.map((r) => [r.ip, r.n]))}</div>
    </section>
  </div>

  <div class="grid">
    <section class="card">
      <h2>总访问量（按站点，已排除机器人）</h2>
      ${renderTable(["站点", "访问次数"], totals.results.map((r) => [r.site, r.n]))}
    </section>
    <section class="card">
      <h2>独立访客数（按 IP 去重）</h2>
      ${renderTable(["站点", "独立 IP 数"], uniqueIps.results.map((r) => [r.site, r.n]))}
    </section>
    <section class="card">
      <h2>访客地区（Top 15）</h2>
      ${renderTable(["国家", "访问次数"], byCountry.results.map((r) => [r.country || "(未知)", r.n]))}
    </section>
    <section class="card">
      <h2>最受访问的页面（Top 20）</h2>
      <div class="scroll">${renderTable(["站点", "路径", "访问次数"], topPaths.results.map((r) => [r.site, r.path, r.n]))}</div>
    </section>
  </div>

  <div class="grid">
    <section class="card wide">
      <h2>每日访问量（最近 30 天）</h2>
      <div class="scroll">${renderTable(["日期", "站点", "访问次数"], byDay.results.map((r) => [r.day, r.site, r.n]))}</div>
    </section>
  </div>

  <div class="grid">
    <section class="card wide">
      <h2>最近 100 条原始记录（含被拦截的）</h2>
      <div class="scroll">${renderTable(
        ["时间", "站点", "状态", "IP", "国家", "城市", "路径", "来源"],
        recent.results.map((r) => [
          r.ts,
          r.site,
          r.blocked ? "🚫 拦截" : "✅ 放行",
          r.ip,
          r.country,
          r.city,
          r.path,
          r.referer || "-",
        ])
      )}</div>
    </section>
  </div>
</main>
</body></html>`;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}
