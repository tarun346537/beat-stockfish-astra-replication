$ErrorActionPreference = 'Stop'
$replicationRoot = $PSScriptRoot
$repository = Join-Path $replicationRoot 'beat-stockfish'
$expectedCommit = '2fe51b6239a6dca70abfd70aca528ff4a0b3c3bf'
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$recordPath = Join-Path $replicationRoot "build-$stamp.json"
$transcriptPath = Join-Path $replicationRoot "build-$stamp.txt"
$record = [ordered]@{
    attribution = 'Replication of Goodhart Labs beat-stockfish; original environment by Goodhart Labs'
    repository = 'https://github.com/Goodhart-Labs/beat-stockfish'
    checkout_commit = $expectedCommit
    started_at = [DateTime]::UtcNow.ToString('o')
    status = 'checking'
    paid_api_calls = 0
    model_episodes_started = 0
    completed_build_stages = @()
}
$previousBuildKit = $env:DOCKER_BUILDKIT
$transcribing = $false
$locationPushed = $false

function Save-BuildRecord {
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath -Encoding UTF8
}

function Assert-PinnedSource {
    $actual = & git -C $repository rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $actual.Trim() -ne $expectedCommit) {
        throw 'Checkout revision differs from the pinned public repository.'
    }
    $changes = & git -C $repository status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0 -or $changes) {
        throw 'Tracked repository files changed. No build or evaluation may continue.'
    }
}

function Build-Stage {
    param([string]$Name, [string[]]$DockerArguments)
    & $dockerExe @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "$Name build failed with exit code $LASTEXITCODE." }
    $record.completed_build_stages += $Name
    Save-BuildRecord
}

try {
    Get-Command git -ErrorAction Stop | Out-Null
    Assert-PinnedSource
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($dockerCommand) {
        $dockerExe = $dockerCommand.Source
    } else {
        $dockerExe = @(
            (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe')
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $dockerExe) { throw 'Docker CLI not found. Complete Docker Desktop installation first.' }
    $serverVersion = & $dockerExe version --format '{{.Server.Version}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not accessible. Open Docker Desktop and wait for its engine.' }
    $containerOS = & $dockerExe info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $containerOS.Trim() -ne 'linux') { throw 'Docker must be running Linux containers.' }
    if ([int]($serverVersion.Trim().Split('.')[0]) -lt 23) { throw 'Docker 23 or newer is required.' }
    & $dockerExe compose version
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is required.' }
    $record.docker_server = $serverVersion.Trim()
    $record.platform = 'linux/amd64'
    $record.build_script_sha256 = (Get-FileHash -LiteralPath (Join-Path $repository 'build.sh') -Algorithm SHA256).Hash.ToLowerInvariant()
    Save-BuildRecord
    Start-Transcript -LiteralPath $transcriptPath | Out-Null
    $transcribing = $true
    Push-Location -LiteralPath $repository
    $locationPushed = $true
    $env:DOCKER_BUILDKIT = '1'
    $record.status = 'building'
    Save-BuildRecord

    # Exact four build stages and arguments from this pinned revision's build.sh.
    Build-Stage -Name 'workstation' -DockerArguments @(
        'build', '--platform', 'linux/amd64', '-t', 'honeyforge-workstation:base', 'bases/workstation'
    )
    Build-Stage -Name 'inspect-runner' -DockerArguments @(
        'build', '--platform', 'linux/amd64', '-t', 'honeyforge-inspect-runner:base',
        '--build-arg', 'WORKSTATION_BASE_IMAGE=honeyforge-workstation:base',
        '--build-context', 'shim-src=bases/inspect-runner/shim-src', 'bases/inspect-runner'
    )
    Build-Stage -Name 'stockfish-base' -DockerArguments @(
        'build', '--platform', 'linux/amd64', '-t', 'honeyforge-beat-stockfish-base:base', 'bases/beat-stockfish'
    )
    Build-Stage -Name 'environment' -DockerArguments @(
        'build', '--platform', 'linux/amd64', '-t', 'beat-stockfish:local', '.'
    )

    Assert-PinnedSource
    $imageId = & $dockerExe image inspect beat-stockfish:local --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0) { throw 'Could not record final image identity.' }
    $record.image_id = $imageId.Trim()
    $record.tracked_source_unchanged = $true
    $record.status = 'BUILD_COMPLETE'
    $record.finished_at = [DateTime]::UtcNow.ToString('o')
    Save-BuildRecord
    $record | ConvertTo-Json -Depth 6
    Write-Host "Saved build record: $recordPath"
}
catch {
    $record.status = 'BUILD_BLOCKED_OR_FAILED'
    $record.error = $_.Exception.Message
    $record.finished_at = [DateTime]::UtcNow.ToString('o')
    Save-BuildRecord
    throw
}
finally {
    if ($locationPushed) { Pop-Location }
    if ($transcribing) { Stop-Transcript | Out-Null }
    $env:DOCKER_BUILDKIT = $previousBuildKit
}
