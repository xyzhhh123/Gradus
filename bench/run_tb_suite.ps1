# Run the Terminal-Bench task list one task at a time, in alphabetical order.
#
#   pwsh -File bench/run_tb_suite.ps1 [-Start 1] [-Count 5] [-Task <name>]
#
# Writes one row per finished task to bench/tb-results.csv and skips tasks that
# already have a row, so it can be stopped and restarted at any time.
#
# The three tasks whose task.toml declares `gpus >= 1` are skipped: they need an
# H100 and cannot run on a CPU-only host.

[CmdletBinding()]
param(
    [int]$Start = 1,
    [int]$Count = 0,
    [string]$Task = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$env:PYTHONPATH = $repo
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PATH = "H:\cache\uv\bin;$env:PATH"

$harbor = "H:\cache\uv\bin\harbor.exe"
$tasksRoot = Join-Path $repo "bench\.cache\tb-tasks\terminal-bench"
$csv = Join-Path $repo "bench\tb-results.csv"

function Get-FreeGB {
    return [math]::Round((Get-PSDrive H).Free / 1GB, 2)
}

function Clear-HarborDocker([bool]$BuildCache) {
    docker container prune -f | Out-Null
    docker image prune -f | Out-Null
    if ($BuildCache) {
        docker builder prune -f | Out-Null
    }
}

function Assert-DiskHeadroom([int]$Completed) {
    $free = Get-FreeGB
    if ($free -lt 12) {
        Write-Host "H: free space is $free GB; pruning Docker cache"
        Clear-HarborDocker $true
        $free = Get-FreeGB
    }
    if ($free -lt 8) {
        throw "H: free space is only $free GB after Docker cleanup; stopping before task $($Completed + 1)"
    }
}

function Get-TaskOutcome([string]$JobName) {
    $outcome = [pscustomobject]@{
        Valid = $false
        Reward = ""
        InputTokens = ""
        OutputTokens = ""
        CacheTokens = ""
        Note = ""
        UsageLimit = $false
    }
    $jobDir = Join-Path $repo "jobs\$JobName"
    $resultPath = Join-Path $jobDir "result.json"
    if (-not (Test-Path $resultPath)) {
        $outcome.Note = "invalid: no result.json"
        return $outcome
    }

    try {
        $r = Get-Content $resultPath -Raw | ConvertFrom-Json
        $outcome.InputTokens = $r.stats.n_input_tokens
        $outcome.OutputTokens = $r.stats.n_output_tokens
        $outcome.CacheTokens = $r.stats.n_cache_tokens
        if (-not $r.finished_at) {
            $outcome.Note = "invalid: run did not finish"
            return $outcome
        }
        if ($r.stats.n_errored_trials -gt 0) {
            $outcome.Note = "invalid: Harbor trial errored"
            return $outcome
        }

        $eval = $r.stats.evals.PSObject.Properties.Value | Select-Object -First 1
        if (-not $eval -or $eval.metrics.Count -eq 0) {
            $outcome.Note = "invalid: no reward"
            return $outcome
        }
        $outcome.Reward = $eval.metrics[0].mean

        $trial = Get-ChildItem $jobDir -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName "result.json") } |
            Select-Object -Last 1
        if (-not $trial) {
            $outcome.Note = "invalid: no trial result"
            return $outcome
        }

        $trialResult = Get-Content (Join-Path $trial.FullName "result.json") -Raw |
            ConvertFrom-Json
        if ($trialResult.exception_info) {
            $exceptionType = $trialResult.exception_info.exception_type
            $outcome.Note = "invalid: $exceptionType"
            return $outcome
        }

        if ($trialResult.agent_result.metadata.gradus_truncated) {
            $outcome.Note = "invalid: response truncated by output limit"
            return $outcome
        }

        $lastError = $trialResult.agent_result.metadata.gradus_error
        if ($lastError -and [double]$outcome.Reward -eq 0) {
            if ($lastError.kind -eq "max_turns") {
                $outcome.Valid = $true
                $outcome.Note = "bounded: max_turns"
                return $outcome
            }
            $message = [string]$lastError.message
            $outcome.UsageLimit = $message -match "GoUsageLimitError"
            $kind = if ($lastError.kind) { $lastError.kind } else { "runtime" }
            $outcome.Note = "invalid: $kind"
            if ($outcome.UsageLimit) {
                $outcome.Note += " usage limit"
            }
            return $outcome
        }

        $outcome.Valid = $true
        return $outcome
    } catch {
        $outcome.Note = "invalid: result parse failed"
        return $outcome
    }
}

# --- task list, alphabetical, GPU tasks removed -----------------------------
$tasks = Get-ChildItem $tasksRoot -Directory | Sort-Object Name | Where-Object {
    $toml = Join-Path $_.FullName "task.toml"
    if (-not (Test-Path $toml)) { return $false }
    $txt = Get-Content $toml -Raw
    $gpus = [regex]::Matches($txt, 'gpus\s*=\s*(\d+)') |
        ForEach-Object { [int]$_.Groups[1].Value } |
        Measure-Object -Maximum
    return ($gpus.Maximum -lt 1)
} | ForEach-Object { $_.Name }

if ($Task) { $tasks = @($Task) }
else {
    $tasks = $tasks | Select-Object -Skip ($Start - 1)
    if ($Count -gt 0) { $tasks = $tasks | Select-Object -First $Count }
}

# --- already finished? ------------------------------------------------------
$done = @{}
if (Test-Path $csv) {
    Import-Csv $csv | ForEach-Object {
        if ($_.note -like "skip:*") {
            $done[$_.task] = $true
            return
        }
        $previous = Get-TaskOutcome $_.job
        if ($previous.Valid) {
            $done[$_.task] = $true
        } else {
            Write-Host "  $($_.task) has no valid result ($($previous.Note)); will retry"
        }
    }
}
if (-not (Test-Path $csv)) {
    "task,job,reward,duration_s,input_tokens,output_tokens,cache_tokens,note" |
        Set-Content -Path $csv -Encoding utf8
}

$total = $tasks.Count
$index = 0

foreach ($name in $tasks) {
    $index++
    if ($done.ContainsKey($name)) {
        Write-Host "[$index/$total] $name — already done, skipping"
        continue
    }

    $taskToml = Join-Path $tasksRoot "$name\task.toml"
    if ((Get-Content $taskToml -Raw) -match 'allow_internet\s*=\s*false') {
        Write-Host "[$index/$total] $name — skip: Harbor Docker does not support no-network mode"
        "$name,tb-$name,,,,,,,skip: Harbor Docker does not support no-network mode" |
            Add-Content -Path $csv -Encoding utf8
        continue
    }

    $job = "tb-" + $name
    Write-Host ""
    Write-Host "[$index/$total] $name — starting $(Get-Date -Format 'HH:mm:ss')"
    $started = Get-Date
    Assert-DiskHeadroom ($index - 1)

    & $harbor run -y -e docker -d "terminal-bench/terminal-bench@4.1.0" `
        -i "terminal-bench/$name" -k 1 `
        -a bench.harbor_agent:GradusAgent -m deepseek-v4.1-flash `
        --ak "max_turns=250" `
        --ak "max_output_tokens=32768" `
        --job-name $job 2>&1 | ForEach-Object { Write-Host "    $_" }

    $elapsed = [int](New-TimeSpan -Start $started -End (Get-Date)).TotalSeconds

    # --- classify the outcome before recording it ---------------------------
    $outcome = Get-TaskOutcome $job
    $reward = $outcome.Reward
    $inputTokens = $outcome.InputTokens
    $outputTokens = $outcome.OutputTokens
    $cacheTokens = $outcome.CacheTokens
    $note = $outcome.Note -replace "[`r`n,]", " "
    if ($note.Length -gt 120) {
        $note = $note.Substring(0, 120)
    }

    "$name,$job,$reward,$elapsed,$inputTokens,$outputTokens,$cacheTokens,$note" |
        Add-Content -Path $csv -Encoding utf8
    Write-Host "[$index/$total] $name — reward=$reward elapsed=${elapsed}s"

    Clear-HarborDocker (($index % 5) -eq 0)
    Write-Host "    H: free after task: $(Get-FreeGB) GB"
    if ($outcome.UsageLimit) {
        Write-Host "Provider quota is exhausted; stopping before task $($index + 1)."
        break
    }
}

Write-Host ""
Write-Host "suite finished. Results: $csv"
