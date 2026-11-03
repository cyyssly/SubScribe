param(
    [int]$Port = 18765,
    [int]$Context = 4096,
    [int]$Parallel = 2,
    [int]$GpuLayers = 99,
    [string]$Model = ""
)

$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root "tools\llama\llama-server.exe"
if (-not $Model) { $Model = Join-Path $root "models\Qwen3-14B-Q4_K_M.gguf" }

if (-not (Test-Path $server)) {
    Write-Host "缺少 llama-server.exe，请重新下载 llama.cpp 的 Windows CUDA 版。" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Model)) {
    Write-Host "缺少模型文件：$Model" -ForegroundColor Red
    Write-Host "请先运行：python scripts/download_models.py --gguf"
    exit 1
}

Write-Host "启动 llama-server：$Model" -ForegroundColor Green
Write-Host "端口 $Port / 上下文 $Context / 并行 $Parallel / GPU 层 $GpuLayers"

& $server -m $Model -ngl $GpuLayers -fa on --jinja -c $Context -np $Parallel --host 127.0.0.1 --port $Port
