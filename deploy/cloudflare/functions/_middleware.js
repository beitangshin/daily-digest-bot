// Cloudflare Pages Function：给每个进来的请求记一条访问日志到 D1，然后照常放行给静态资源。
//
// 部署时这份源码会先用 `wrangler pages functions build` 编译成单个 _worker.js，
// 再复制到 output_deploy(_news)/_worker.js（见 run_daily_digest.ps1）—— 必须编译成
// _worker.js 放在部署目录根部（Pages "Advanced Mode"），不能直接放一份 functions/ 目录
// 进去指望 `wrangler pages deploy` 自动识别：实测这个 wrangler 版本（4.120.0）在用
// --branch/--commit-dirty 参数时会跳过 functions/ 目录自动打包（wrangler 内部日志
// "Pages-to-Workers delegation skipped"），导致 Function 完全不生效但也不报错，很隐蔽。
// _worker.js 这条路径没有这个问题，编译产物不进 git（deploy/cloudflare/_worker_build/
// 在 .gitignore 里，跟 output_deploy 一样是每次部署前现生成的构建产物）。
//
// 统计数据自己看的入口是 admin.js（/admin，Basic Auth 保护），这里只管写库 + 拦截机器人。
//
// 反爬拦截：命中 _bot.js 的判定就直接 403，不放行到静态资源，同时把这次尝试也记进 visits
// 表（blocked=1），这样 /admin 能看到"拦了多少、拦的是谁"，而不是拦完就当没发生过。
// /admin 路径本身跳过这整套逻辑（不判定也不拦截）：它已经有 Basic Auth 保护，双重拦截没
// 意义，而且我们自己经常用 curl/脚本去戳 /admin 测试，被自己的反爬拦下来会很烦。
// /robots.txt 也跳过 -- 这个文件存在的意义就是让机器人读到"请不要抓取"，如果连读取
// robots.txt 本身都因为 UA 像机器人而被拦，等于自己把自己的告示牌挡住了，遵守规则的
// 爬虫反而看不到"该走开"这个信号（不遵守规则的爬虫反正也不会被这一条拦住，无所谓）。
const EXEMPT_PATHS = new Set(["/admin", "/robots.txt"]);

import { isBot } from "./_bot.js";

export async function onRequest(context) {
  const { request, env, next } = context;
  const url = new URL(request.url);

  if (EXEMPT_PATHS.has(url.pathname)) return next();

  const botReason = isBot(request);

  try {
    const cf = request.cf || {};
    await env.VISITS_DB.prepare(
      "INSERT INTO visits (ts, site, ip, country, city, path, referer, user_agent, blocked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
      .bind(
        new Date().toISOString(),
        env.SITE_NAME || "unknown",
        request.headers.get("CF-Connecting-IP"),
        cf.country || null,
        cf.city || null,
        url.pathname,
        request.headers.get("Referer") || null,
        request.headers.get("User-Agent") || null,
        botReason ? 1 : 0
      )
      .run();
  } catch (err) {
    // 记录失败绝不能影响正常访问，吞掉就行。
    console.error("visit logging failed", err);
  }

  if (botReason) {
    return new Response("Forbidden", { status: 403 });
  }

  return next();
}
