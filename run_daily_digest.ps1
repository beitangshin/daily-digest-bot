param()

$ErrorActionPreference = "Continue"
$root = "D:\projects\daily-digest-bot"
Set-Location $root
$mainLog = Join-Path $root "output\run.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $mainLog -Value $line -Encoding UTF8
}

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

Log "======== 全部完成 ========"
