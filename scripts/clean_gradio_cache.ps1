param(
    [switch]$Quiet
)

# 清空 Gradio 上传缓存目录。字幕工具的旧上传视频不会被再次使用，
# 每次启动前清理可避免缓存长期占用磁盘。

$ErrorActionPreference = "Stop"
$dir = Join-Path $env:TEMP "gradio"

if (-not (Test-Path -LiteralPath $dir)) {
    if (-not $Quiet) {
        Write-Host "缓存目录不存在，无需清理。"
    }
    exit 0
}

# 字幕工具正在运行时跳过，避免中断当前页面正在使用的视频
$inUse = $false
try {
    $client = New-Object Net.Sockets.TcpClient
    $client.Connect("127.0.0.1", 7860)
    $client.Close()
    $inUse = $true
} catch {
}

if ($inUse) {
    if (-not $Quiet) {
        Write-Host "字幕工具正在运行，请先关闭程序窗口后再清理缓存。" -ForegroundColor Yellow
    }
    exit 1
}

$drive = (Get-Item -LiteralPath $dir).PSDrive.Name
$before = (Get-PSDrive -Name $drive).Free

$items = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)
$removed = 0
$failed = @()

foreach ($item in $items) {
    try {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
        $removed++
    } catch {
        $failed += $item.Name
    }
}

$after = (Get-PSDrive -Name $drive).Free

if (-not $Quiet) {
    Write-Host ("已清理 {0} 项，释放 {1:N1} MB" -f $removed, (($after - $before) / 1MB))
    if ($failed.Count -gt 0) {
        Write-Host ("有 {0} 项未能删除（可能正被占用）：{1}" -f $failed.Count, ($failed -join "、")) -ForegroundColor Yellow
    }
}

exit 0
