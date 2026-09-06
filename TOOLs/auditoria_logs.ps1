$ErrorActionPreference = 'Stop'

# Docker/Compose sometimes writes progress to stderr even on exit code 0.
# Native-command stderr must not be promoted to a terminating PowerShell error.
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$ComposeFile = $env:HERMES_COMPOSE_FILE
$Service     = $env:HERMES_DOCKER_SERVICE
$WorkersDir  = $env:HERMES_WORKERS_LOG_DIR
$RuntimeDir  = $env:HERMES_RUNTIME_LOG_DIR
$KanbanDir   = $env:HERMES_KANBAN_LOG_DIR
$KanbanDbContainerPath = $env:HERMES_KANBAN_DB_CONTAINER_PATH
$AuditDir    = $env:HERMES_AUDIT_LOG_DIR
$GwrmLog     = $env:GWRM_LOG_FILE

$GovernorEventsPath = '/opt/data/profiles/execution-governor/governance/events.jsonl'
$GovernorStatePath  = '/opt/data/profiles/execution-governor/governance/state.json'
$GovernorCasesPath  = '/opt/data/profiles/execution-governor/governance/cases'

$KanbanTailSeconds = 2
$Warnings = New-Object 'System.Collections.Generic.List[string]'

function Add-Warn {
    param([string]$Message)
    $script:Warnings.Add($Message)
    Write-Warning $Message
}

function Ensure-Dir {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Required path variable is empty.'
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Copy-DirContents {
    param([string]$Source, [string]$Destination)

    Ensure-Dir $Destination
    if (-not (Test-Path -LiteralPath $Source)) {
        Add-Warn "Directory not found: $Source"
        return
    }

    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        try {
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
        }
        catch {
            Add-Warn "Failed to copy '$($_.FullName)': $($_.Exception.Message)"
        }
    }
}

function Copy-RuntimeLogs {
    param([string]$Source, [string]$Destination)

    Ensure-Dir $Destination
    if (-not (Test-Path -LiteralPath $Source)) {
        Add-Warn "Runtime log directory not found: $Source"
        return
    }

    $excludedNames = @('gateways', 'curator', 'git-trace2')

    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.PSIsContainer -and ($excludedNames -contains $_.Name.ToLowerInvariant())) {
            Write-Host ("[COPY] Skipping runtime\{0}" -f $_.Name)
            return
        }

        try {
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
        }
        catch {
            Add-Warn "Failed to copy runtime item '$($_.FullName)': $($_.Exception.Message)"
        }
    }
}

function Copy-ContainerItem {
    param(
        [string]$ContainerSource,
        [string]$DestinationDirectory,
        [string]$Label
    )

    Ensure-Dir $DestinationDirectory

    $sourceSpec = "${Service}:$ContainerSource"
    $dockerArgs = @(
        'compose',
        '-f', $ComposeFile,
        'cp',
        $sourceSpec,
        $DestinationDirectory
    )

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.x can promote native stderr records to terminating
        # errors when the global preference is Stop. docker compose cp writes
        # normal "Copying/Copied" progress to stderr even with exit code 0.
        $ErrorActionPreference = 'Continue'

        $output = @(& docker @dockerArgs 2>&1)
        $exitCode = $LASTEXITCODE

        if ($exitCode -ne 0) {
            $details = ($output -join ' ').Trim()
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = 'no additional output'
            }
            Add-Warn "Failed to collect ${Label} from ${ContainerSource} (docker compose cp exit code ${exitCode}): $details"
            return $false
        }

        return $true
    }
    catch {
        Add-Warn "Failed to collect ${Label} from ${ContainerSource}: $($_.Exception.Message)"
        return $false
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
}


function Export-GovernorCasesJsonl {
    param(
        [string]$ContainerCasesDirectory,
        [string]$DestinationDirectory
    )

    Ensure-Dir $DestinationDirectory

    $aggregatorScript = @'
from __future__ import annotations

import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])

if not source.is_dir():
    raise FileNotFoundError(f"Governance cases directory not found: {source}")

files = sorted(path for path in source.glob("*.json") if path.is_file())
parse_errors = 0

output.parent.mkdir(parents=True, exist_ok=True)
with output.open("w", encoding="utf-8", newline="\n") as handle:
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="utf-8", errors="replace")

        try:
            case = json.loads(raw)
            record = {
                "source_file": path.name,
                "case": case,
            }
        except Exception as exc:
            parse_errors += 1
            record = {
                "source_file": path.name,
                "parse_error": f"{type(exc).__name__}: {exc}",
                "raw": raw,
            }

        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

print(f"cases={len(files)}")
print(f"parse_errors={parse_errors}")
'@

    $scriptBytes = [System.Text.Encoding]::UTF8.GetBytes($aggregatorScript)
    $scriptBase64 = [Convert]::ToBase64String($scriptBytes)
    $containerOutputDir = "/tmp/hermes-audit-governance-$PID"
    $containerOutputFile = "$containerOutputDir/cases.jsonl"
    $destinationFile = Join-Path $DestinationDirectory 'cases.jsonl'
    $diagnosticFile = Join-Path $DestinationDirectory 'cases_aggregation_diagnostic.txt'

    $command = "rm -rf '$containerOutputDir'; mkdir -p '$containerOutputDir'; " +
               "printf '%s' '$scriptBase64' | base64 -d | python3 - " +
               "'$ContainerCasesDirectory' '$containerOutputFile'"

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'

        $execArgs = @(
            'compose',
            '-f', $ComposeFile,
            'exec', '-T',
            $Service,
            'sh', '-lc', $command
        )

        $execOutput = @(& docker @execArgs 2>&1)
        $execExit = $LASTEXITCODE

        @(
            "Source: $ContainerCasesDirectory"
            "Aggregator exit: $execExit"
            ($execOutput -join [Environment]::NewLine)
        ) | Out-File -LiteralPath $diagnosticFile -Encoding utf8 -Force

        if ($execExit -ne 0) {
            Add-Warn "Failed to aggregate Execution Governor cases (exit $execExit). See $diagnosticFile"
            return $false
        }

        $copyArgs = @(
            'compose',
            '-f', $ComposeFile,
            'cp',
            "${Service}:${containerOutputFile}",
            $DestinationDirectory
        )

        $copyOutput = @(& docker @copyArgs 2>&1)
        $copyExit = $LASTEXITCODE

        Add-Content -LiteralPath $diagnosticFile -Value (
            "`nCOPY cases.jsonl exit=$copyExit`n" +
            ($copyOutput -join [Environment]::NewLine)
        )

        if ($copyExit -ne 0) {
            Add-Warn "Failed to copy aggregated Execution Governor cases (exit $copyExit)."
            return $false
        }

        if (-not (Test-Path -LiteralPath $destinationFile)) {
            Add-Warn "Aggregated Execution Governor cases copy returned success but cases.jsonl is missing."
            return $false
        }

        return $true
    }
    catch {
        Add-Warn "Failed to aggregate Execution Governor cases: $($_.Exception.Message)"
        return $false
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference

        $cleanupArgs = @(
            'compose',
            '-f', $ComposeFile,
            'exec', '-T',
            $Service,
            'rm', '-rf', $containerOutputDir
        )
        try {
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            & docker @cleanupArgs *> $null
            $ErrorActionPreference = $old
        }
        catch {
            # /tmp cleanup is best effort.
        }
    }
}


function Export-HermesRuntimeMetadata {
    param([string]$Destination)

    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add("GeneratedAt=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
    $lines.Add("ComposeFile=$ComposeFile")
    $lines.Add("Service=$Service")

    try {
        $versionArgs = @('compose', '-f', $ComposeFile, 'exec', '-T', $Service, 'hermes', '--version')
        $versionOutput = @(& docker @versionArgs 2>&1)
        $lines.Add("HermesVersion=$($versionOutput -join ' ')")
    }
    catch {
        $lines.Add("HermesVersion=<failed: $($_.Exception.Message)>")
    }

    try {
        $containerIdArgs = @('compose', '-f', $ComposeFile, 'ps', '-q', $Service)
        $containerIdOutput = @(& docker @containerIdArgs 2>&1)
        $containerId = (($containerIdOutput | Select-Object -First 1) -as [string]).Trim()
        $lines.Add("ContainerId=$containerId")

        if (-not [string]::IsNullOrWhiteSpace($containerId)) {
            $imageName = @(& docker inspect $containerId --format '{{.Config.Image}}' 2>&1)
            $imageId = @(& docker inspect $containerId --format '{{.Image}}' 2>&1)
            $lines.Add("ImageName=$($imageName -join ' ')")
            $lines.Add("ImageId=$($imageId -join ' ')")
        }
    }
    catch {
        $lines.Add("ContainerMetadata=<failed: $($_.Exception.Message)>")
    }

    try {
        $gitArgs = @(
            'compose', '-f', $ComposeFile,
            'exec', '-T', $Service,
            'git', '-C', '/opt/hermes', 'rev-parse', 'HEAD'
        )
        $gitOutput = @(& docker @gitArgs 2>&1)
        if ($LASTEXITCODE -eq 0) {
            $lines.Add("HermesSourceGitHead=$($gitOutput -join ' ')")
        }
        else {
            $lines.Add("HermesSourceGitHead=<not a git checkout>")
        }
    }
    catch {
        $lines.Add("HermesSourceGitHead=<failed: $($_.Exception.Message)>")
    }

    [System.IO.File]::WriteAllLines(
        $Destination,
        $lines,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Read-FilePrefix {
    param([string]$Path, [int]$MaxBytes = 131072)

    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        try {
            $length = [Math]::Min([int64]$MaxBytes, $stream.Length)
            $buffer = New-Object byte[] ([int]$length)
            [void]$stream.Read($buffer, 0, $buffer.Length)
            return [System.Text.Encoding]::UTF8.GetString($buffer)
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        return ''
    }
}

function Get-WorkerTaskIds {
    param([string]$Directory)

    if (-not (Test-Path -LiteralPath $Directory)) {
        Add-Warn "Workers log directory not found: $Directory"
        return @()
    }

    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $files = @(Get-ChildItem -LiteralPath $Directory -File -Recurse -Force -ErrorAction SilentlyContinue)

    foreach ($file in $files) {
        $foundInPath = $false

        foreach ($match in [regex]::Matches($file.FullName, '(?i)(t_[A-Za-z0-9_-]+)')) {
            [void]$ids.Add($match.Groups[1].Value)
            $foundInPath = $true
        }

        if (-not $foundInPath) {
            $prefix = Read-FilePrefix -Path $file.FullName
            $patterns = @(
                '(?im)^\s*Query:\s*work\s+kanban\s+task\s+(t_[A-Za-z0-9_-]+)\b',
                '(?im)^\s*(?:task_id|card_id)\s*[:=]\s*(t_[A-Za-z0-9_-]+)\b'
            )
            foreach ($pattern in $patterns) {
                foreach ($match in [regex]::Matches($prefix, $pattern)) {
                    [void]$ids.Add($match.Groups[1].Value)
                }
            }
        }
    }

    return @($ids | Sort-Object)
}

function Export-KanbanTail {
    param([string]$TaskId, [string]$Destination)

    $shellCommand = "PYTHONUNBUFFERED=1 timeout ${KanbanTailSeconds}s hermes kanban tail $TaskId"
    $dockerArgs = @(
        'compose',
        '-f', $ComposeFile,
        'exec', '-T',
        $Service,
        'sh', '-lc', $shellCommand
    )

    try {
        $output = @(& docker @dockerArgs 2>&1)
        $exitCode = $LASTEXITCODE
        $text = $output -join [Environment]::NewLine

        $opened = $text -match ("(?im)^Tailing events for\s+" + [regex]::Escape($TaskId) + "\.")
        if (-not $opened) {
            Add-Warn "Card $TaskId could not be opened by 'hermes kanban tail'. It will not be included in the ZIP."
            return $false
        }

        if ($exitCode -notin @(0, 124, 137, 143)) {
            Add-Warn "kanban tail for $TaskId returned exit code $exitCode, but event history was captured."
        }

        $text | Out-File -LiteralPath $Destination -Encoding utf8 -Force
        return $true
    }
    catch {
        Add-Warn "Failed to export Kanban events for ${TaskId}: $($_.Exception.Message)"
        return $false
    }
}


function Export-KanbanDatabaseSnapshot {
    param(
        [string[]]$TaskIds,
        [string]$Destination
    )

    Ensure-Dir $Destination
    $boardDestination = Join-Path $Destination 'default'
    Ensure-Dir $boardDestination

    if ([string]::IsNullOrWhiteSpace($KanbanDbContainerPath)) {
        $script:KanbanDbContainerPath = '/var/lib/hermes-kanban/kanban.db'
    }

    # Deliberately mirrors the user's proven backup-kanban.bat:
    # inline base64 Python -> sqlite3.backup() in container -> docker compose cp.
    $exporterBase64 = 'CmZyb20gX19mdXR1cmVfXyBpbXBvcnQgYW5ub3RhdGlvbnMKCmltcG9ydCBoYXNobGliCmltcG9ydCBqc29uCmltcG9ydCBvcwppbXBvcnQgc3FsaXRlMwppbXBvcnQgc3lzCmZyb20gcGF0aGxpYiBpbXBvcnQgUGF0aAoKc291cmNlID0gUGF0aChzeXMuYXJndlsxXSkKb3V0cHV0X3Jvb3QgPSBQYXRoKHN5cy5hcmd2WzJdKQp0YXNrX2lkcyA9IFt4IGZvciB4IGluIHN5cy5hcmd2WzNdLnNwbGl0KCIsIikgaWYgeF0gaWYgbGVuKHN5cy5hcmd2KSA+IDMgZWxzZSBbXQoKaWYgbm90IHNvdXJjZS5pc19maWxlKCk6CiAgICByYWlzZSBGaWxlTm90Rm91bmRFcnJvcihmIkthbmJhbiBEQiBub3QgZm91bmQ6IHtzb3VyY2V9IikKCm91dHB1dF9yb290Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKc25hcHNob3QgPSBvdXRwdXRfcm9vdCAvICJrYW5iYW4uZGIiCgppZiBzbmFwc2hvdC5leGlzdHMoKToKICAgIHNuYXBzaG90LnVubGluaygpCgpzcmMgPSBzcWxpdGUzLmNvbm5lY3QoCiAgICBmImZpbGU6e3NvdXJjZX0/bW9kZT1ybyIsCiAgICB1cmk9VHJ1ZSwKICAgIHRpbWVvdXQ9MzAsCikKc3JjLmV4ZWN1dGUoIlBSQUdNQSBxdWVyeV9vbmx5ID0gT04iKQpzcmMuZXhlY3V0ZSgiUFJBR01BIGJ1c3lfdGltZW91dCA9IDMwMDAwIikKCmRzdCA9IHNxbGl0ZTMuY29ubmVjdChzdHIoc25hcHNob3QpLCB0aW1lb3V0PTMwKQp0cnk6CiAgICBzcmMuYmFja3VwKGRzdCwgcGFnZXM9MjU2LCBzbGVlcD0wLjA1KQogICAgZHN0LmNvbW1pdCgpCgogICAgaW50ZWdyaXR5ID0gZHN0LmV4ZWN1dGUoIlBSQUdNQSBpbnRlZ3JpdHlfY2hlY2siKS5mZXRjaGFsbCgpCiAgICBxdWljayA9IGRzdC5leGVjdXRlKCJQUkFHTUEgcXVpY2tfY2hlY2siKS5mZXRjaGFsbCgpCgogICAgaWYgaW50ZWdyaXR5ICE9IFsoIm9rIiwpXToKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoZiJpbnRlZ3JpdHlfY2hlY2sgZmFpbGVkOiB7aW50ZWdyaXR5fSIpCiAgICBpZiBxdWljayAhPSBbKCJvayIsKV06CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKGYicXVpY2tfY2hlY2sgZmFpbGVkOiB7cXVpY2t9IikKZmluYWxseToKICAgIGRzdC5jbG9zZSgpCiAgICBzcmMuY2xvc2UoKQoKd2l0aCBzbmFwc2hvdC5vcGVuKCJyYisiKSBhcyBoYW5kbGU6CiAgICBoYW5kbGUuZmx1c2goKQogICAgb3MuZnN5bmMoaGFuZGxlLmZpbGVubygpKQoKZGlnZXN0ID0gaGFzaGxpYi5zaGEyNTYoc25hcHNob3QucmVhZF9ieXRlcygpKS5oZXhkaWdlc3QoKQoKY29ubiA9IHNxbGl0ZTMuY29ubmVjdChzdHIoc25hcHNob3QpKQpjb25uLnJvd19mYWN0b3J5ID0gc3FsaXRlMy5Sb3cKCmRlZiBxaWRlbnQobmFtZTogc3RyKSAtPiBzdHI6CiAgICByZXR1cm4gJyInICsgbmFtZS5yZXBsYWNlKCciJywgJyIiJykgKyAnIicKCmRlZiByb3dfZGljdChyb3cpOgogICAgb3V0ID0ge30KICAgIGZvciBrZXkgaW4gcm93LmtleXMoKToKICAgICAgICB2YWx1ZSA9IHJvd1trZXldCiAgICAgICAgaWYgaXNpbnN0YW5jZSh2YWx1ZSwgYnl0ZXMpOgogICAgICAgICAgICB2YWx1ZSA9IHsiX19ieXRlc19oZXhfXyI6IHZhbHVlLmhleCgpfQogICAgICAgIG91dFtrZXldID0gdmFsdWUKICAgIHJldHVybiBvdXQKCmRlZiBjb2x1bW5zKHRhYmxlOiBzdHIpOgogICAgc2FmZSA9IHRhYmxlLnJlcGxhY2UoJyInLCAnIiInKQogICAgcmV0dXJuIFtzdHIocm93WzFdKSBmb3Igcm93IGluIGNvbm4uZXhlY3V0ZShmJ1BSQUdNQSB0YWJsZV9pbmZvKCJ7c2FmZX0iKScpXQoKZGVmIHNlbGVjdGVkX3Jvd3ModGFibGU6IHN0ciwgY29sczogbGlzdFtzdHJdKToKICAgIGlmIG5vdCB0YXNrX2lkczoKICAgICAgICByZXR1cm4gW10KCiAgICBwcmVkaWNhdGVzID0gW10KICAgIHBhcmFtcyA9IFtdCgogICAgaWYgdGFibGUgPT0gInRhc2tzIiBhbmQgImlkIiBpbiBjb2xzOgogICAgICAgIG1hcmtzID0gIiwiLmpvaW4oIj8iIGZvciBfIGluIHRhc2tfaWRzKQogICAgICAgIHByZWRpY2F0ZXMuYXBwZW5kKGYne3FpZGVudCgiaWQiKX0gSU4gKHttYXJrc30pJykKICAgICAgICBwYXJhbXMuZXh0ZW5kKHRhc2tfaWRzKQoKICAgIHJlbGF0aW9uX2NvbHMgPSBbCiAgICAgICAgYyBmb3IgYyBpbiBjb2xzCiAgICAgICAgaWYgYyA9PSAidGFza19pZCIKICAgICAgICBvciBjLmVuZHN3aXRoKCJfdGFza19pZCIpCiAgICAgICAgb3IgYyBpbiB7InBhcmVudF9pZCIsICJjaGlsZF9pZCJ9CiAgICBdCiAgICBmb3IgY29sIGluIHJlbGF0aW9uX2NvbHM6CiAgICAgICAgbWFya3MgPSAiLCIuam9pbigiPyIgZm9yIF8gaW4gdGFza19pZHMpCiAgICAgICAgcHJlZGljYXRlcy5hcHBlbmQoZid7cWlkZW50KGNvbCl9IElOICh7bWFya3N9KScpCiAgICAgICAgcGFyYW1zLmV4dGVuZCh0YXNrX2lkcykKCiAgICBpZiBub3QgcHJlZGljYXRlczoKICAgICAgICByZXR1cm4gW10KCiAgICBzcWwgPSBmIlNFTEVDVCAqIEZST00ge3FpZGVudCh0YWJsZSl9IFdIRVJFICIgKyAiIE9SICIuam9pbihwcmVkaWNhdGVzKQogICAgb3JkZXJfY29scyA9IFtjIGZvciBjIGluICgiaWQiLCAiY3JlYXRlZF9hdCIsICJ0aW1lc3RhbXAiLCAidHMiKSBpZiBjIGluIGNvbHNdCiAgICBpZiBvcmRlcl9jb2xzOgogICAgICAgIHNxbCArPSAiIE9SREVSIEJZICIgKyAiLCAiLmpvaW4ocWlkZW50KGMpIGZvciBjIGluIG9yZGVyX2NvbHMpCgogICAgcmV0dXJuIFtyb3dfZGljdChyb3cpIGZvciByb3cgaW4gY29ubi5leGVjdXRlKHNxbCwgcGFyYW1zKV0KCnRyeToKICAgIHRhYmxlcyA9IFsKICAgICAgICBzdHIocm93WzBdKQogICAgICAgIGZvciByb3cgaW4gY29ubi5leGVjdXRlKAogICAgICAgICAgICAiU0VMRUNUIG5hbWUgRlJPTSBzcWxpdGVfbWFzdGVyICIKICAgICAgICAgICAgIldIRVJFIHR5cGU9J3RhYmxlJyBBTkQgbmFtZSBOT1QgTElLRSAnc3FsaXRlXyUnICIKICAgICAgICAgICAgIk9SREVSIEJZIG5hbWUiCiAgICAgICAgKQogICAgXQoKICAgIHNjaGVtYSA9IHt9CiAgICB0YWJsZV9jb3VudHMgPSB7fQogICAgc2VsZWN0ZWQgPSB7fQoKICAgIGZvciB0YWJsZSBpbiB0YWJsZXM6CiAgICAgICAgY29scyA9IGNvbHVtbnModGFibGUpCiAgICAgICAgc2NoZW1hW3RhYmxlXSA9IGNvbHMKICAgICAgICB0YWJsZV9jb3VudHNbdGFibGVdID0gaW50KAogICAgICAgICAgICBjb25uLmV4ZWN1dGUoZiJTRUxFQ1QgQ09VTlQoKikgRlJPTSB7cWlkZW50KHRhYmxlKX0iKS5mZXRjaG9uZSgpWzBdCiAgICAgICAgKQogICAgICAgIHJvd3MgPSBzZWxlY3RlZF9yb3dzKHRhYmxlLCBjb2xzKQogICAgICAgIGlmIHJvd3M6CiAgICAgICAgICAgIHNlbGVjdGVkW3RhYmxlXSA9IHJvd3MKCiAgICAob3V0cHV0X3Jvb3QgLyAic2NoZW1hLmpzb24iKS53cml0ZV90ZXh0KAogICAgICAgIGpzb24uZHVtcHMoCiAgICAgICAgICAgIHsKICAgICAgICAgICAgICAgICJzb3VyY2VfZGIiOiBzdHIoc291cmNlKSwKICAgICAgICAgICAgICAgICJzbmFwc2hvdF9kYiI6IHN0cihzbmFwc2hvdCksCiAgICAgICAgICAgICAgICAic2hhMjU2IjogZGlnZXN0LAogICAgICAgICAgICAgICAgInRhYmxlcyI6IHNjaGVtYSwKICAgICAgICAgICAgICAgICJ0YWJsZV9jb3VudHMiOiB0YWJsZV9jb3VudHMsCiAgICAgICAgICAgIH0sCiAgICAgICAgICAgIGVuc3VyZV9hc2NpaT1GYWxzZSwKICAgICAgICAgICAgaW5kZW50PTIsCiAgICAgICAgKSArICJcbiIsCiAgICAgICAgZW5jb2Rpbmc9InV0Zi04IiwKICAgICkKCiAgICAob3V0cHV0X3Jvb3QgLyAic2VsZWN0ZWRfY2FyZHMuanNvbiIpLndyaXRlX3RleHQoCiAgICAgICAganNvbi5kdW1wcygKICAgICAgICAgICAgewogICAgICAgICAgICAgICAgInJlcXVlc3RlZF90YXNrX2lkcyI6IHRhc2tfaWRzLAogICAgICAgICAgICAgICAgInRhYmxlcyI6IHNlbGVjdGVkLAogICAgICAgICAgICB9LAogICAgICAgICAgICBlbnN1cmVfYXNjaWk9RmFsc2UsCiAgICAgICAgICAgIGluZGVudD0yLAogICAgICAgICkgKyAiXG4iLAogICAgICAgIGVuY29kaW5nPSJ1dGYtOCIsCiAgICApCgogICAgd2l0aCAob3V0cHV0X3Jvb3QgLyAidGFza19ldmVudHMuanNvbmwiKS5vcGVuKCJ3IiwgZW5jb2Rpbmc9InV0Zi04IikgYXMgZjoKICAgICAgICBmb3Igcm93IGluIHNlbGVjdGVkLmdldCgidGFza19ldmVudHMiLCBbXSk6CiAgICAgICAgICAgIGYud3JpdGUoanNvbi5kdW1wcyhyb3csIGVuc3VyZV9hc2NpaT1GYWxzZSkgKyAiXG4iKQoKICAgIHdpdGggKG91dHB1dF9yb290IC8gInRhc2tfY29tbWVudHMuanNvbmwiKS5vcGVuKCJ3IiwgZW5jb2Rpbmc9InV0Zi04IikgYXMgZjoKICAgICAgICBmb3Igcm93IGluIHNlbGVjdGVkLmdldCgidGFza19jb21tZW50cyIsIFtdKToKICAgICAgICAgICAgZi53cml0ZShqc29uLmR1bXBzKHJvdywgZW5zdXJlX2FzY2lpPUZhbHNlKSArICJcbiIpCgogICAgaW5kZXggPSB7CiAgICAgICAgInNvdXJjZV9kYiI6IHN0cihzb3VyY2UpLAogICAgICAgICJzbmFwc2hvdF9kYiI6IHN0cihzbmFwc2hvdCksCiAgICAgICAgInNoYTI1NiI6IGRpZ2VzdCwKICAgICAgICAic2l6ZSI6IHNuYXBzaG90LnN0YXQoKS5zdF9zaXplLAogICAgICAgICJyZXF1ZXN0ZWRfdGFza19pZHMiOiB0YXNrX2lkcywKICAgICAgICAidGFibGVfY291bnRzIjogdGFibGVfY291bnRzLAogICAgICAgICJzZWxlY3RlZF90YWJsZXMiOiBzb3J0ZWQoc2VsZWN0ZWQua2V5cygpKSwKICAgICAgICAic2VsZWN0ZWRfZXZlbnRfY291bnQiOiBsZW4oc2VsZWN0ZWQuZ2V0KCJ0YXNrX2V2ZW50cyIsIFtdKSksCiAgICAgICAgInNlbGVjdGVkX2NvbW1lbnRfY291bnQiOiBsZW4oc2VsZWN0ZWQuZ2V0KCJ0YXNrX2NvbW1lbnRzIiwgW10pKSwKICAgICAgICAiaW50ZWdyaXR5X2NoZWNrIjogIm9rIiwKICAgICAgICAicXVpY2tfY2hlY2siOiAib2siLAogICAgfQogICAgKG91dHB1dF9yb290IC8gImluZGV4Lmpzb24iKS53cml0ZV90ZXh0KAogICAgICAgIGpzb24uZHVtcHMoaW5kZXgsIGVuc3VyZV9hc2NpaT1GYWxzZSwgaW5kZW50PTIpICsgIlxuIiwKICAgICAgICBlbmNvZGluZz0idXRmLTgiLAogICAgKQoKICAgIHByaW50KCJpbnRlZ3JpdHk9b2siKQogICAgcHJpbnQoInF1aWNrX2NoZWNrPW9rIikKICAgIHByaW50KGYidGFza3M9e3RhYmxlX2NvdW50cy5nZXQoJ3Rhc2tzJywgMCl9IikKICAgIHByaW50KGYic2l6ZT17c25hcHNob3Quc3RhdCgpLnN0X3NpemV9IikKICAgIHByaW50KGYic2hhMjU2PXtkaWdlc3R9IikKICAgIHByaW50KGYic2VsZWN0ZWRfZXZlbnRzPXtpbmRleFsnc2VsZWN0ZWRfZXZlbnRfY291bnQnXX0iKQogICAgcHJpbnQoZiJzZWxlY3RlZF9jb21tZW50cz17aW5kZXhbJ3NlbGVjdGVkX2NvbW1lbnRfY291bnQnXX0iKQpmaW5hbGx5OgogICAgY29ubi5jbG9zZSgpCg=='
    $containerOutput = "/tmp/hermes-audit-kanban-$PID"
    $taskIdArgument = $TaskIds -join ','
    $diagnosticFile = Join-Path $Destination 'snapshot_diagnostic.txt'

    $command = "rm -rf '$containerOutput'; mkdir -p '$containerOutput'; " +
               "printf '%s' '$exporterBase64' | base64 -d | python3 - " +
               "'$KanbanDbContainerPath' '$containerOutput' '$taskIdArgument'"

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        # Native programs can emit normal progress on stderr. We judge them by exit code.
        $ErrorActionPreference = 'Continue'

        $execArgs = @(
            'compose',
            '-f', $ComposeFile,
            'exec', '-T', '-u', '10000:10000',
            $Service,
            'sh', '-c', $command
        )

        $execOutput = @(& docker @execArgs 2>&1)
        $execExit = $LASTEXITCODE

        @(
            "Kanban DB path: $KanbanDbContainerPath"
            "Exporter exit: $execExit"
            ($execOutput -join [Environment]::NewLine)
        ) | Out-File -LiteralPath $diagnosticFile -Encoding utf8 -Force

        if ($execExit -ne 0) {
            Add-Warn "Kanban SQLite exporter failed (exit $execExit). See $diagnosticFile"
            return $false
        }

        $files = @(
            'kanban.db',
            'schema.json',
            'selected_cards.json',
            'task_events.jsonl',
            'task_comments.jsonl',
            'index.json'
        )

        foreach ($name in $files) {
            $sourceSpec = "${Service}:${containerOutput}/$name"
            $destinationFile = Join-Path $boardDestination $name

            $copyArgs = @(
                'compose',
                '-f', $ComposeFile,
                'cp',
                $sourceSpec,
                $destinationFile
            )

            # Do NOT use --quiet: the user's Docker Compose path is proven with plain cp.
            $copyOutput = @(& docker @copyArgs 2>&1)
            $copyExit = $LASTEXITCODE

            Add-Content -LiteralPath $diagnosticFile -Value (
                "`nCOPY $name exit=$copyExit`n" +
                ($copyOutput -join [Environment]::NewLine)
            )

            if ($copyExit -ne 0) {
                Add-Warn "Failed to copy Kanban artifact $name (exit $copyExit)."
                return $false
            }

            if (-not (Test-Path -LiteralPath $destinationFile)) {
                Add-Warn "docker compose cp returned success but $destinationFile is missing."
                return $false
            }
        }

        $dbPath = Join-Path $boardDestination 'kanban.db'
        $dbInfo = Get-Item -LiteralPath $dbPath
        if ($dbInfo.Length -le 0) {
            Add-Warn "Kanban snapshot exists but is empty: $dbPath"
            return $false
        }

        # Also expose index.json at db_snapshot root for quick inspection.
        Copy-Item -LiteralPath (Join-Path $boardDestination 'index.json') `
                  -Destination (Join-Path $Destination 'index.json') -Force

        return $true
    }
    catch {
        Add-Warn "Failed to export Kanban DB/events snapshot: $($_.Exception.Message)"
        try {
            Add-Content -LiteralPath $diagnosticFile -Value (
                "`nPOWERSHELL_EXCEPTION`n" + $_.Exception.ToString()
            )
        }
        catch {
            # best effort only
        }
        return $false
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference

        $cleanupArgs = @(
            'compose',
            '-f', $ComposeFile,
            'exec', '-T', '-u', '10000:10000',
            $Service,
            'rm', '-rf', $containerOutput
        )
        try {
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            & docker @cleanupArgs *> $null
            $ErrorActionPreference = $old
        }
        catch {
            # /tmp cleanup is best effort.
        }
    }
}

foreach ($required in @(
    @{ Name='HERMES_COMPOSE_FILE'; Value=$ComposeFile },
    @{ Name='HERMES_DOCKER_SERVICE'; Value=$Service },
    @{ Name='HERMES_WORKERS_LOG_DIR'; Value=$WorkersDir },
    @{ Name='HERMES_RUNTIME_LOG_DIR'; Value=$RuntimeDir },
    @{ Name='HERMES_KANBAN_LOG_DIR'; Value=$KanbanDir },
    @{ Name='HERMES_AUDIT_LOG_DIR'; Value=$AuditDir },
    @{ Name='GWRM_LOG_FILE'; Value=$GwrmLog }
)) {
    if ([string]::IsNullOrWhiteSpace($required.Value)) {
        throw "Required environment variable is empty: $($required.Name)"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker was not found in PATH.'
}
if (-not (Test-Path -LiteralPath $ComposeFile)) {
    throw "docker-compose.yml not found: $ComposeFile"
}

Ensure-Dir $KanbanDir
Ensure-Dir $AuditDir

Write-Host ''
Write-Host '============================================================'
Write-Host ' Hermes - Audit log collection'
Write-Host '============================================================'
Write-Host ''

Write-Host '[WORKERS] Reading card IDs from worker logs...'
$taskIds = @(Get-WorkerTaskIds -Directory $WorkersDir)

if ($taskIds.Count -eq 0) {
    Add-Warn 'No card IDs were found in worker logs. No Kanban card event logs will be exported.'
}
else {
    Write-Host ("[WORKERS] Eligible cards: {0}" -f ($taskIds -join ', '))
}

$timestamp = Get-Date -Format 'HHmm_ddMMyyyy'
$zipName   = "auditoria_${timestamp}.zip"
$zipPath   = Join-Path $AuditDir $zipName
$staging   = Join-Path $env:TEMP ("hermes_audit_{0}_{1}" -f $timestamp, $PID)

if (Test-Path -LiteralPath $staging) {
    Remove-Item -LiteralPath $staging -Recurse -Force
}
Ensure-Dir $staging

Write-Host '[HERMES] Capturing runtime/image identity...'
Export-HermesRuntimeMetadata -Destination (Join-Path $staging 'hermes_runtime_metadata.txt')

$stagingWorkers = Join-Path $staging 'workers'
$stagingRuntime = Join-Path $staging 'runtime'
$stagingKanban  = Join-Path $staging 'kanban'
$stagingKanbanDb = Join-Path $stagingKanban 'db_snapshot'
$stagingGwrm       = Join-Path $staging 'gwrm'
$stagingGovernance = Join-Path $staging 'governance'
Ensure-Dir $stagingKanban

$capturedKanbanIds = New-Object 'System.Collections.Generic.List[string]'

foreach ($taskId in $taskIds) {
    Write-Host ("[KANBAN] Exporting {0}..." -f $taskId)

    $persistentFile = Join-Path $KanbanDir ($taskId + '.log')
    $stagingFile    = Join-Path $stagingKanban ($taskId + '.log')

    if (Export-KanbanTail -TaskId $taskId -Destination $persistentFile) {
        Copy-Item -LiteralPath $persistentFile -Destination $stagingFile -Force
        $capturedKanbanIds.Add($taskId)
    }
}

Write-Host '[KANBAN] Creating consistent SQLite snapshot + task events/comments...'
$kanbanDbSnapshotCaptured = Export-KanbanDatabaseSnapshot -TaskIds $taskIds -Destination $stagingKanbanDb

Write-Host '[COPY] Worker logs...'
Copy-DirContents $WorkersDir $stagingWorkers

Write-Host '[COPY] Hermes runtime logs (excluding gateways, curator and git-trace2)...'
Copy-RuntimeLogs $RuntimeDir $stagingRuntime

Write-Host '[COPY] GWRM log...'
Ensure-Dir $stagingGwrm
if (Test-Path -LiteralPath $GwrmLog) {
    try {
        Copy-Item -LiteralPath $GwrmLog -Destination (Join-Path $stagingGwrm 'gwrm.log') -Force
    }
    catch {
        Add-Warn "Failed to copy GWRM log: $($_.Exception.Message)"
    }
}
else {
    Add-Warn "GWRM log not found: $GwrmLog"
}

Write-Host '[COPY] Execution Governor governance audit (events + state + aggregated cases)...'
Ensure-Dir $stagingGovernance
[void](Copy-ContainerItem -ContainerSource $GovernorEventsPath -DestinationDirectory $stagingGovernance -Label 'Execution Governor events')
[void](Copy-ContainerItem -ContainerSource $GovernorStatePath -DestinationDirectory $stagingGovernance -Label 'Execution Governor state')
$governorCasesAggregated = Export-GovernorCasesJsonl -ContainerCasesDirectory $GovernorCasesPath -DestinationDirectory $stagingGovernance

$manifestPath = Join-Path $staging 'manifesto_auditoria.txt'
$manifest = @(
    'Hermes - Audit package'
    ('Generated at: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'))
    ''
    'Kanban filter rule:'
    '  Only card IDs found in the worker log directory are queried.'
    '  Only cards successfully opened by hermes kanban tail are included in this ZIP.'
    ''
    'Sources:'
    ('  workers -> ' + $WorkersDir)
    ('  runtime -> ' + $RuntimeDir + ' (excluding gateways, curator and git-trace2)')
    ('  kanban  -> ' + $KanbanDir)
    '  kanban DB/events -> consistent SQLite backup from /var/lib/hermes-kanban/kanban.db (configurable via HERMES_KANBAN_DB_CONTAINER_PATH)'
    ('  gwrm    -> ' + $GwrmLog)
    ('  governor events -> ' + $GovernorEventsPath)
    ('  governor state  -> ' + $GovernorStatePath)
    ('  governor cases  -> ' + $GovernorCasesPath + ' (aggregated into governance/cases.jsonl)')
    ('  governor cases aggregation captured: ' + ($(if ($governorCasesAggregated) { 'yes' } else { 'no' })))
    ''
    ('Compose file: ' + $ComposeFile)
    ('Docker service: ' + $Service)
    ('Worker card IDs: ' + ($(if ($taskIds.Count) { $taskIds -join ', ' } else { '(none)' })))
    ('Kanban card IDs captured now: ' + ($(if ($capturedKanbanIds.Count) { $capturedKanbanIds -join ', ' } else { '(none)' })))
    ('Kanban SQLite snapshot captured: ' + ($(if ($kanbanDbSnapshotCaptured) { 'yes' } else { 'no' })))
    'Kanban DB snapshot contents: kanban/db_snapshot/default/kanban.db + schema.json + selected_cards.json + task_events.jsonl + task_comments.jsonl + index.json'
)

if ($Warnings.Count -gt 0) {
    $manifest += ''
    $manifest += 'Warnings:'
    foreach ($warningText in $Warnings) {
        $manifest += ('  - ' + $warningText)
    }
}
else {
    $manifest += ''
    $manifest += 'Warnings: none.'
}

$manifest | Out-File -LiteralPath $manifestPath -Encoding utf8 -Force

Write-Host '[ZIP] Creating audit package...'
try {
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }

    Compress-Archive `
        -Path (Join-Path $staging '*') `
        -DestinationPath $zipPath `
        -CompressionLevel Optimal `
        -Force
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ''
Write-Host '============================================================'
Write-Host ' Audit completed'
Write-Host '============================================================'
Write-Host ("ZIP: {0}" -f $zipPath)

if ($Warnings.Count -gt 0) {
    Write-Host ("Completed with {0} warning(s). See manifesto_auditoria.txt inside the ZIP." -f $Warnings.Count)
}

exit 0
