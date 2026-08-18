param()

$ErrorActionPreference = "Continue"
$root = "D:\projects\daily-digest-bot"
Set-Location $root
$mainLog = Join-Path $root "output\run.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $mainLog -Value $line -Encoding UTF8
}

# .env isn't auto-loaded into the process environment the way python-dotenv
# loads it for the Python side -- wrangler (a separate node process) needs
# CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID set as real env vars.
function Load-DotEnv($path) {
    if (-not (Test-Path $path)) { return }
    Get-Content $path | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $_ -notmatch '^\s*#') {
            $name = $Matches[1]
            $value = $Matches[2] -replace '^"(.*)"$', '$1'
            if ($value) { Set-Item -Path "Env:$name" -Value $value }
        }
    }
}
Load-DotEnv (Join-Path $root ".env")

Log "======== 开始全频道运行（并发） ========"

$jobs = @()

$jobs += Start-Job -Name "ai" -ScriptBlock {
    param($dir)
    Set-Location $dir
    $env:PYTHONIOENCODING = "utf-8"
    python -m daily_digest.cli run --channel ai 2>&1
} -ArgumentList $root

$jobs += Start-Job -Name "autonomous_driving" -ScriptBlock {
    param($dir)
    Set-Location $dir
    $env:PYTHONIOENCODING = "utf-8"
    python -m daily_digest.cli run --channel autonomous_driving 2>&1
} -ArgumentList $root

$jobs += Start-Job -Name "housing" -ScriptBlock {
    param($dir)
    Set-Location $dir
    $env:PYTHONIOENCODING = "utf-8"
    python housing_digest.py -o output\housing\latest.html 2>&1
} -ArgumentList $root

Wait-Job -Job $jobs | Out-Null

foreach ($j in $jobs) {
    Log "--- $($j.Name) ---"
    $output = Receive-Job -Job $j
    if ($output) {
        $output | ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
    }
    Remove-Job -Job $j
}

Log "--- 部署到 Cloudflare Pages（daily-digest-bot，全部频道，含房源）---"
try {
    $deployDir = Join-Path $root "output_deploy"
    # /MIR keeps output_deploy an exact mirror of output/ each run; the /XF
    # names are internal working files (listings cache, run logs, per-channel
    # dedup state) that have no reason to be public -- excluded files are
    # never copied or touched, so this list is safe to extend later.
    robocopy $root\output $deployDir /MIR /NFL /NDL /NJH /NJS /XF *.log .state.json .listings_db.json | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Log "robocopy 镜像 output/ 失败，exit=$LASTEXITCODE，跳过本次发布"
    } else {
        # 访客记录 + /admin 统计面板这两个 Pages Function 必须编译成 _worker.js 放在部署
        # 目录根部（Advanced Mode），不能直接扔一份 functions/ 目录进去指望 wrangler 自动
        # 识别 -- 实测这个 wrangler 版本配合 --branch/--commit-dirty 参数时会静默跳过
        # functions/ 目录打包（Function 完全不生效，但部署本身不报错，非常隐蔽，见
        # _middleware.js 顶部注释）。
        # 编译前必须先删掉上一轮遗留的 _worker.js -- 亲测如果 --build-output-directory
        # 里已经有一个 _worker.js，wrangler 会直接复用那份旧编译产物而不是从 functions/
        # 重新构建，新加的路由（比如 admin.js）会静默消失，问题现象和上面那条一样隐蔽。
        # 正常流程里 /MIR 已经会清掉它（output/ 里本来就没有 _worker.js），这里再删一次
        # 是双重保险，不依赖执行顺序假设。
        Remove-Item -Path (Join-Path $deployDir "_worker.js") -Force -ErrorAction SilentlyContinue
        # 编译产物不进 git，每次部署前现生成，跟 output_deploy 一样是构建产物。输出文件名
        # 不总是 index.js（观察到过 _worker.js），所以按"这个目录下唯一的 .js 文件"取，
        # 不硬编码文件名。
        $fnSrc = Join-Path $root "deploy\cloudflare\functions"
        $workerBuildDir = Join-Path $root "deploy\cloudflare\_worker_build"
        Remove-Item -Path $workerBuildDir -Recurse -Force -ErrorAction SilentlyContinue
        $outdirArg = "--outdir=$workerBuildDir"
        $buildOutDirArg = "--build-output-directory=$deployDir"
        npx wrangler pages functions build $fnSrc $outdirArg $buildOutDirArg 2>&1 |
            ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
        $builtWorker = Get-ChildItem -Path $workerBuildDir -Filter *.js | Select-Object -First 1
        if (-not $builtWorker) {
            Log "编译 Pages Function 失败，$workerBuildDir 下没有产物，跳过本次发布"
        } else {
        Copy-Item -Path $builtWorker.FullName -Destination (Join-Path $deployDir "_worker.js") -Force
        $deployOut = npx wrangler pages deploy $deployDir --project-name=daily-digest-bot --branch=main --commit-dirty=true 2>&1
        $deployOut | ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
        if ($LASTEXITCODE -ne 0) {
            Log "Cloudflare Pages 发布失败，exit=$LASTEXITCODE"
        } else {
            Log "Cloudflare Pages 发布成功"
        }
        }
    }
} catch {
    Log "部署步骤异常: $_"
}

Log "--- 部署到 Cloudflare Pages（daily-digest-news，仅 AI + 自动驾驶，不含房源）---"
try {
    $newsDir = Join-Path $root "output_deploy_news"
    # 分别镜像这两个频道各自的目录到 output_deploy_news 下同名子目录，housing/ 从不
    # 被复制到这里 -- 这是"不含房源"这个约束在文件层面的实现方式，而不是发布后再过滤。
    robocopy $root\output\ai $newsDir\ai /MIR /NFL /NDL /NJH /NJS /XF *.log .state.json .listings_db.json | Out-Null
    $roboAi = $LASTEXITCODE
    robocopy $root\output\autonomous_driving $newsDir\autonomous_driving /MIR /NFL /NDL /NJH /NJS /XF *.log .state.json .listings_db.json | Out-Null
    $roboAd = $LASTEXITCODE
    if ($roboAi -ge 8 -or $roboAd -ge 8) {
        Log "robocopy 镜像 output_deploy_news 失败，exit=$roboAi/$roboAd，跳过本次发布"
    } else {
        # 只重新拼 index.html（读两个频道各自的 meta.json），不重新抓取/摘要。
        python -m daily_digest.cli render-index --output-dir $newsDir --channels ai,autonomous_driving 2>&1 |
            ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
        # 同一份 functions/ 源码，两个项目各自的 SITE_NAME 环境变量（在 Cloudflare 项目
        # 设置里配的，不在这份代码里）区分是哪个站点的访问；--build-output-directory 不同
        # 所以两边各自单独编译一次 _worker.js，不复用 daily-digest-bot 那份构建产物。
        # 编译前删掉遗留的 _worker.js / 构建目录，原因同上一个部署块的注释（否则 wrangler
        # 可能直接复用旧编译产物，新路由静默消失）。
        Remove-Item -Path (Join-Path $newsDir "_worker.js") -Force -ErrorAction SilentlyContinue
        $fnSrcNews = Join-Path $root "deploy\cloudflare\functions"
        $workerBuildDirNews = Join-Path $root "deploy\cloudflare\_worker_build_news"
        Remove-Item -Path $workerBuildDirNews -Recurse -Force -ErrorAction SilentlyContinue
        $outdirArgNews = "--outdir=$workerBuildDirNews"
        $buildOutDirArgNews = "--build-output-directory=$newsDir"
        npx wrangler pages functions build $fnSrcNews $outdirArgNews $buildOutDirArgNews 2>&1 |
            ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
        $builtWorkerNews = Get-ChildItem -Path $workerBuildDirNews -Filter *.js | Select-Object -First 1
        if (-not $builtWorkerNews) {
            Log "编译 Pages Function 失败（news），$workerBuildDirNews 下没有产物，跳过本次发布"
        } else {
        Copy-Item -Path $builtWorkerNews.FullName -Destination (Join-Path $newsDir "_worker.js") -Force
        $deployOut2 = npx wrangler pages deploy $newsDir --project-name=daily-digest-news --branch=main --commit-dirty=true 2>&1
        $deployOut2 | ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
        if ($LASTEXITCODE -ne 0) {
            Log "Cloudflare Pages（daily-digest-news）发布失败，exit=$LASTEXITCODE"
        } else {
            Log "Cloudflare Pages（daily-digest-news）发布成功"
        }
        }
    }
} catch {
    Log "部署步骤（news）异常: $_"
}

Log "--- 清理 30 天前的访客记录（D1: daily-digest-visits）---"
try {
    $cutoff = (Get-Date).ToUniversalTime().AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $cleanupOut = npx wrangler d1 execute daily-digest-visits --remote --command="DELETE FROM visits WHERE ts < '$cutoff'" 2>&1
    $cleanupOut | ForEach-Object { Add-Content -Path $mainLog -Value $_ -Encoding UTF8 }
} catch {
    Log "访客记录清理异常: $_"
}

Log "======== 全部完成 ========"
