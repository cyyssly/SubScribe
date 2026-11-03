param(
    [switch]$Elevated,
    [string]$CacheDir,
    [string]$ProjectDir
)

# 把 Gradio 上传缓存目录和项目目录加入 Windows Defender 排除项，
# 避免刚上传的大视频被实时扫描占用，导致预览加载缓慢或读取失败。
#
# 普通权限启动时会检查本地标记；标记缺失时通过 UAC 提权运行一次本脚本，
# 由管理员进程完成添加，成功后再由普通进程写入标记，之后启动不再弹窗。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$cache = Join-Path $env:TEMP "gradio"
$markerDir = Join-Path $env:LOCALAPPDATA "SubScribe"
$markerFile = Join-Path $markerDir "defender_excluded.json"

function Add-ExclusionPaths([string[]]$paths) {
    try {
        $prefs = Get-MpPreference -ErrorAction Stop
    } catch {
        # Defender 未启用或已由第三方安全软件接管，无需添加排除项
        return 0
    }

    try {
        $normalized = @()
        if ($prefs.ExclusionPath) {
            foreach ($item in @($prefs.ExclusionPath)) {
                if ($item) {
                    $normalized += $item.TrimEnd('\')
                }
            }
        }
        foreach ($path in $paths) {
            if (-not (Test-Path -LiteralPath $path)) {
                New-Item -ItemType Directory -Path $path -Force | Out-Null
            }
            if ($path.TrimEnd('\') -notin $normalized) {
                Add-MpPreference -ExclusionPath $path -ErrorAction Stop
            }
        }
    } catch {
        return 3
    }

    return 0
}

function Test-Marker {
    if (-not (Test-Path -LiteralPath $markerFile)) {
        return $false
    }
    try {
        $marker = Get-Content -LiteralPath $markerFile -Raw | ConvertFrom-Json
        return (
            $marker.computer -eq $env:COMPUTERNAME -and
            $marker.root -eq $root -and
            $marker.temp -eq $cache
        )
    } catch {
        return $false
    }
}

function Write-Marker {
    if (-not (Test-Path -LiteralPath $markerDir)) {
        New-Item -ItemType Directory -Path $markerDir -Force | Out-Null
    }
    $payload = [ordered]@{
        computer = $env:COMPUTERNAME
        root = $root
        temp = $cache
        time = (Get-Date).ToString("s")
    }
    $json = $payload | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($markerFile, $json, (New-Object Text.UTF8Encoding($false)))
}

if ($Elevated) {
    # 管理员分支：只负责添加排除项，结果通过退出码返回给普通进程
    if (-not $CacheDir) {
        $CacheDir = $cache
    }
    if (-not $ProjectDir) {
        $ProjectDir = $root
    }
    exit (Add-ExclusionPaths @($CacheDir, $ProjectDir))
}

if (Test-Marker) {
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if ($isAdmin) {
    # 直接以管理员身份启动（例如右键“以管理员身份运行”）
    $code = Add-ExclusionPaths @($cache, $root)
    if ($code -eq 0) {
        try {
            Write-Marker
        } catch {
        }
    }
    exit $code
}

# 普通权限：先提权添加，成功后由当前用户写入标记
try {
    $self = '"{0}"' -f $MyInvocation.MyCommand.Path
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $self,
        "-Elevated",
        "-CacheDir", ('"{0}"' -f $cache),
        "-ProjectDir", ('"{0}"' -f $root)
    )
    $proc = Start-Process powershell -Verb RunAs -PassThru -Wait -ArgumentList $arguments
} catch {
    # 用户取消了 UAC 授权
    exit 2
}

if ($proc.ExitCode -eq 0) {
    try {
        Write-Marker
    } catch {
    }
}

exit $proc.ExitCode
