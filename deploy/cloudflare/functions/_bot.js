// 共享的机器人判定逻辑，被 _middleware.js（拦截）和 admin.js（统计展示）复用。
// 文件名以下划线开头 -- Cloudflare Pages Functions 的约定是下划线开头的文件/目录不算路由
// （跟 _middleware.js 是同一套规则），所以这个文件不会被当成某个 URL 路径的 handler。
//
// 判定标准刻意保守：只挡"明显是脚本/工具/扫描器"的请求，不挡任何看起来像真实浏览器的
// User-Agent。误伤主要风险是挡到你自己用 curl/脚本测试的请求 -- /admin 本身在
// _middleware.js 里被排除在拦截之外（反正它有 Basic Auth 保护），其余路径如果需要用
// curl 测试，加一个 `-A "Mozilla/5.0"` 就绕过去了。

const BOT_UA_RE =
  /bot|crawl|spider|scrape|slurp|fetch|curl|wget|python-requests|python-urllib|go-http-client|java\/|libwww|httpclient|okhttp|scrapy|headlesschrome|phantomjs|node-fetch|axios\/|postman|insomnia|masscan|nmap|nikto|sqlmap|zgrab|censys|shodan|semrush|ahrefs|mj12bot|dotbot|petalbot|bytespider/i;

export function isBot(request) {
  const ua = request.headers.get("User-Agent") || "";
  if (!ua) return "empty-ua"; // 真实浏览器几乎不会不带 User-Agent
  if (BOT_UA_RE.test(ua)) return "bot-ua";
  return null;
}
