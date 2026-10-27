$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $RepoRoot "storage\logs"
$ApiUrl = "http://127.0.0.1:8001"
$ApiHealthUrl = "$ApiUrl/health"
$TimeoutSeconds = 60

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction Stop
}
$npmPath = $npmCommand.Source

$started = @()

function Test-HttpOk {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Stop-StartedProcesses {
    foreach ($process in $started) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-ForHttpOk {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [System.Diagnostics.Process]$Process = $null
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk -Url $Url) {
            Write-Output "$Name is healthy at $Url"
            return
        }
        if ($Process -and $Process.HasExited) {
            throw "$Name exited before $Url returned HTTP 200. Check logs in $LogDir."
        }
        Start-Sleep -Seconds 1
    }

    throw "$Name did not return HTTP 200 at $Url within $TimeoutSeconds seconds. Check logs in $LogDir."
}

function Get-LumenDesktopWindowProcess {
    return Get-Process -Name electron -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq "Lumen" } |
        Select-Object -First 1
}

function Wait-ForDesktopWindow {
    param([System.Diagnostics.Process]$Process = $null)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $windowProcess = Get-LumenDesktopWindowProcess
        if ($windowProcess) {
            Write-Output "Lumen desktop is ready (pid=$($windowProcess.Id))."
            return
        }
        if ($Process -and $Process.HasExited) {
            throw "Lumen desktop exited before its window opened. Check logs in $LogDir."
        }
        Start-Sleep -Seconds 1
    }

    throw "Lumen desktop did not open within $TimeoutSeconds seconds. Check logs in $LogDir."
}

try {
    if (Test-HttpOk -Url $ApiHealthUrl) {
        Write-Output "API is already healthy at $ApiHealthUrl"
    } else {
        $apiOut = Join-Path $LogDir "api-dev.out.log"
        $apiErr = Join-Path $LogDir "api-dev.err.log"
        Write-Output "Starting API on 127.0.0.1:8001..."
        $apiProcess = Start-Process -FilePath $npmPath -ArgumentList @("run", "api:dev") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr -PassThru
        $started += $apiProcess
        Wait-ForHttpOk -Name "API" -Url $ApiHealthUrl -Process $apiProcess
    }

    if (Get-LumenDesktopWindowProcess) {
        Write-Output "Lumen desktop is already running."
    } else {
        $desktopOut = Join-Path $LogDir "desktop-dev.out.log"
        $desktopErr = Join-Path $LogDir "desktop-dev.err.log"
        Write-Output "Starting Lumen desktop..."
        $desktopProcess = Start-Process -FilePath $npmPath -ArgumentList @("run", "desktop:dev") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $desktopOut -RedirectStandardError $desktopErr -PassThru
        $started += $desktopProcess
        Wait-ForDesktopWindow -Process $desktopProcess
    }

    Write-Output "Lumen desktop stack is ready."
    Write-Output "API: $ApiUrl"
    Write-Output "Desktop: Lumen"
    Write-Output "Logs: $LogDir"
} catch {
    Write-Error $_
    Stop-StartedProcesses
    exit 1
}
