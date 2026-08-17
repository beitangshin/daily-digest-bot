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
// 只写库，不对外提供任何查询/展示接口 —— 想看数据自己用
// `wrangler d1 execute daily-digest-visits --remote --command "..."` 查，不做公开页面。
export async function onRequest(context) {
  const { request, env, next } = context;

  try {
    const url = new URL(request.url);
    const cf = request.cf || {};
    await env.VISITS_DB.prepare(
      "INSERT INTO visits (ts, site, ip, country, city, path, referer) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
      .bind(
        new Date().toISOString(),
        env.SITE_NAME || "unknown",
        request.headers.get("CF-Connecting-IP"),
        cf.country || null,
        cf.city || null,
        url.pathname,
        request.headers.get("Referer") || null
      )
      .run();
  } catch (err) {
    // 记录失败绝不能影响正常访问，吞掉就行。
    console.error("visit logging failed", err);
  }

  return next();
}
