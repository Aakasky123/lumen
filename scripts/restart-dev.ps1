$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoRoot "storage\logs"
$ApiUrl = "http://127.0.0.1:8001"
$ApiHealthUrl = "$ApiUrl/health"
$Ports = @(8001) + @(3210..3219)
$TimeoutSeconds = 70

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StatusLog = Join-Path $LogDir "restart-status.log"
Set-Content -LiteralPath $StatusLog -Value "Lumen restart started: $((Get-Date).ToString("s"))"

function Report {
    param([Parameter(Mandatory = $true)][string]$Message)

    Add-Content -LiteralPath $StatusLog -Value $Message
    Write-Host $Message
}

function Get-CommandPath {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    throw "None of these commands were found: $($Names -join ', ')"
}

$NpmPath = Get-CommandPath -Names @("npm.cmd", "npm")
$PythonPath = Get-CommandPath -Names @("python.exe", "python")

function Test-HttpOk {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-ForHttpOk {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][datetime]$Deadline
    )

    while ((Get-Date) -lt $Deadline) {
        if (Test-HttpOk -Url $Url) {
            Report "$Name ready: $Url"
            return
        }

        Start-Sleep -Seconds 1
    }

    throw "$Name did not return HTTP 200 at $Url within $TimeoutSeconds seconds."
}

function Test-LumenDevCommand {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    $cmd = $CommandLine.ToLowerInvariant()
    $repo = $RepoRoot.ToLowerInvariant()

    if ($cmd.Contains("uvicorn") -and $cmd.Contains("app.main:app") -and $cmd.Contains("--port 8001")) {
        return $true
    }
    if ($cmd.Contains("desktop:dev") -and $cmd.Contains($repo)) {
        return $true
    }
    if ($cmd.Contains("apps\desktop\scripts\dev.mjs") -and $cmd.Contains($repo)) {
        return $true
    }
    if ($cmd.Contains("apps/desktop/scripts/dev.mjs") -and $cmd.Contains($repo)) {
        return $true
    }
    if ($cmd.Contains("electron.exe") -and $cmd.Contains("--app-path") -and $cmd.Contains("apps\desktop")) {
        return $true
    }
    return $false
}

function Stop-LumenDevStack {
    $allProcesses = @(Get-CimInstance Win32_Process)
    $byId = @{}
    foreach ($process in $allProcesses) {
        $byId[[int]$process.ProcessId] = $process
    }

    $protectedIds = New-Object System.Collections.Generic.HashSet[int]
    $currentId = [int]$PID
    while ($currentId -and $byId.ContainsKey($currentId)) {
        [void]$protectedIds.Add($currentId)
        $currentId = [int]$byId[$currentId].ParentProcessId
    }

    $listenerIds = @(
        Get-NetTCPConnection -State Listen -LocalPort $Ports -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -ErrorAction SilentlyContinue
    )
    $commandIds = @(
        $allProcesses |
            Where-Object { $_.ProcessId -ne $PID -and (Test-LumenDevCommand -CommandLine $_.CommandLine) } |
            Select-Object -ExpandProperty ProcessId
    )

    $targetIds = New-Object System.Collections.Generic.HashSet[int]
    foreach ($id in @($listenerIds + $commandIds)) {
        if ($id -and -not $protectedIds.Contains([int]$id)) {
            [void]$targetIds.Add([int]$id)
        }
    }

    $changed = $true
    while ($changed) {
        $changed = $false

        foreach ($process in $allProcesses) {
            if ($protectedIds.Contains([int]$process.ProcessId)) {
                continue
            }
            if ($targetIds.Contains([int]$process.ParentProcessId) -and $targetIds.Add([int]$process.ProcessId)) {
                $changed = $true
            }
        }
    }

    $stopped = @($targetIds) | Sort-Object -Descending
    foreach ($id in $stopped) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }

    $deadline = (Get-Date).AddSeconds(12)
    do {
        $remaining = @(Get-NetTCPConnection -State Listen -LocalPort $Ports -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    $remaining = @(Get-NetTCPConnection -State Listen -LocalPort $Ports -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) {
        $details = ($remaining | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) pid=$($_.OwningProcess)" }) -join "; "
        throw "Could not clear dev ports: $details"
    }

    if ($stopped.Count -gt 0) {
        Report "Stopped dev processes: $((@($stopped) | Sort-Object) -join ', ')"
    } else {
        Report "No existing dev processes found."
    }
}

function Start-LumenDevStack {
    $apiOut = Join-Path $LogDir "api-restart.out.log"
    $apiErr = Join-Path $LogDir "api-restart.err.log"
    $desktopOut = Join-Path $LogDir "desktop-restart.out.log"
    $desktopErr = Join-Path $LogDir "desktop-restart.err.log"

    Remove-Item -LiteralPath $apiOut, $apiErr, $desktopOut, $desktopErr -ErrorAction SilentlyContinue

    Start-Process -FilePath $NpmPath -ArgumentList @("run", "api:dev") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr | Out-Null
    Start-Sleep -Seconds 2
    Start-Process -FilePath $NpmPath -ArgumentList @("run", "desktop:dev") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $desktopOut -RedirectStandardError $desktopErr | Out-Null

    Report "Logs: $LogDir"
}

function Get-LumenDesktopWindowProcess {
    return Get-Process -Name electron -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq "Lumen" } |
        Select-Object -First 1
}

function Wait-ForDesktopWindow {
    param([Parameter(Mandatory = $true)][datetime]$Deadline)

    while ((Get-Date) -lt $Deadline) {
        $windowProcess = Get-LumenDesktopWindowProcess
        if ($windowProcess) {
            Report "Lumen desktop ready: pid=$($windowProcess.Id)"
            return
        }

        Start-Sleep -Seconds 1
    }

    throw "Lumen desktop did not open within $TimeoutSeconds seconds."
}

function Invoke-ModeCheck {
    $modeScript = Join-Path $RepoRoot "apps\api\mode_check.py"
    $modeOutput = & $PythonPath $modeScript
    if ($LASTEXITCODE -ne 0) {
        throw "Mode check failed with exit code $LASTEXITCODE. Output: $($modeOutput -join ' ')"
    }

    $mode = ($modeOutput -join "`n") | ConvertFrom-Json
    $warnings = @($mode.warnings) -join ", "
    if ([string]::IsNullOrWhiteSpace($warnings)) {
        $warnings = "none"
    }

    Report "Mode: model_mode=$($mode.model_mode), profile_mode=$($mode.profile_mode), resume_mode=$($mode.resume_mode)"
    Report "Warnings: $warnings"
}

Stop-LumenDevStack
Start-LumenDevStack

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
Wait-ForHttpOk -Name "API" -Url $ApiHealthUrl -Deadline $deadline
Wait-ForDesktopWindow -Deadline $deadline
Invoke-ModeCheck

$listeners = @(
    Get-NetTCPConnection -State Listen -LocalPort $Ports -ErrorAction SilentlyContinue |
        Sort-Object LocalPort |
        ForEach-Object { "$($_.LocalPort)=pid:$($_.OwningProcess)" }
)

Report "Listeners: $($listeners -join ', ')"
Report "Lumen desktop stack restarted."
