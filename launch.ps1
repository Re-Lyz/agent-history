param(
    [Parameter(Position=0)][string]$Profile,
    [Alias('h')][switch]$Help,
    [switch]$List,
    [switch]$History,
    [string]$From,
    [string]$Session,
    [switch]$Pick,
    [string]$Query = '',
    [ValidateRange(1,10000)][int]$Limit = 30,
    [switch]$PrepareOnly,
    [switch]$NoSync,
    [switch]$SyncHistory,
    [string[]]$CodexArgs = @()
)
$ErrorActionPreference = 'Stop'
$sharedRoot = if ($env:AGENT_HISTORY_SHARED_ROOT) { $env:AGENT_HISTORY_SHARED_ROOT } else { Split-Path -Parent $PSScriptRoot }
if ($Help -or $Profile -eq 'help') {
    Get-Content -LiteralPath (Join-Path $PSScriptRoot 'HELP.md') -Raw -Encoding UTF8
    return
}
$registry = Get-Content -LiteralPath (Join-Path $sharedRoot 'connections.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$pythonExe = $env:AGENT_HISTORY_PYTHON
if (-not $pythonExe) {
    $pythonExe = Join-Path (Split-Path -Parent $sharedRoot) 'anaconda3\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $pythonExe = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
}
$agentHistoryTool = Join-Path $PSScriptRoot 'agent_history.py'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
if ($From -match '^\d+$' -and $registry.connections.PSObject.Properties["openai$From"]) {
    $From = "openai$From"
}
if ($List) {
    $registry.connections.PSObject.Properties | ForEach-Object {
        [pscustomobject]@{Name=$_.Name; Home=$_.Value.home; HistoryOnly=[bool]$_.Value.history_only}
    }
    return
}
if ($History) {
    $historyArgs = @($agentHistoryTool, 'list-codex', '--limit', "$Limit", '--include-imports')
    if ($From) { $historyArgs += @('--source', $From) }
    if ($Query) { $historyArgs += @('--query', $Query) }
    & $pythonExe @historyArgs
    if ($LASTEXITCODE -ne 0) { throw 'History lookup failed.' }
    return
}
if (-not $Profile) { throw 'Specify a connection or use -List / -History.' }
$property = $registry.connections.PSObject.Properties[$Profile]
if (-not $property) { throw 'Unknown connection. Use -List to see available names.' }
$entry = $property.Value
if ($entry.history_only) { throw 'This connection is registered for history lookup only.' }
if ([IO.Path]::IsPathRooted($entry.home)) { $target = $entry.home }
else { $target = [IO.Path]::GetFullPath((Join-Path $sharedRoot $entry.home)) }
if ($SyncHistory) {
    $syncArgs = @($agentHistoryTool, 'sync-codex', '--to', $Profile)
    if ($PrepareOnly) { $syncArgs += '--dry-run' }
    & $pythonExe @syncArgs
    if ($LASTEXITCODE -ne 0) { throw 'Native history sync failed; see the reported source and error.' }
    return
}
$profileConfig = Join-Path $target 'profile.toml'
$common = Join-Path $sharedRoot 'common.toml'
$rootOnlyKeys = @('model','model_provider','review_model','model_reasoning_effort','model_catalog_json','forced_login_method','windows_wsl_setup_acknowledged')
foreach ($file in @($profileConfig, $common)) {
    $seenTable = $false
    foreach ($line in Get-Content -LiteralPath $file -Encoding UTF8) {
        if ($line -match '^\s*\[') { $seenTable = $true; continue }
        if ($seenTable) {
            foreach ($key in $rootOnlyKeys) {
                if ($line -match "^\s*$([regex]::Escape($key))\s*=") { throw "Root key '$key' appears after a table in $file" }
            }
        }
    }
}
$launchArgs = @($entry.launch_args)
$record = $null
if ($Pick -and $Session) { throw 'Use -Pick or -Session, not both.' }
if ($Pick -or ($From -and -not $Session)) {
    if (-not $From) { $From = $Profile }
    $pickerArgs = @($agentHistoryTool, 'list-codex', '--source', $From, '--limit', "$Limit", '--json')
    if ($From -eq $Profile) { $pickerArgs += '--include-imports' }
    if ($Query) { $pickerArgs += @('--query', $Query) }
    $pickerJson = & $pythonExe @pickerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not load conversation choices.' }
    $choices = @(($pickerJson | ConvertFrom-Json) | ForEach-Object { $_ })
    if ($choices.Count -eq 0) { Write-Host "No matching conversations in $From."; return }
    Write-Host "Conversations from $From (most recent first):"
    for ($index = 0; $index -lt $choices.Count; $index++) {
        $choice = $choices[$index]
        $date = ([datetime]'1970-01-01T00:00:00Z').ToUniversalTime().AddSeconds($choice.modified).ToLocalTime().ToString('MM-dd HH:mm')
        $label = if ($choice.title) { $choice.title } else { $choice.cwd }
        $label = ($label -replace '[\x00-\x1f\x7f]', ' ')
        if ($label.Length -gt 100) { $label = $label.Substring(0,100) + '...' }
        $suffix = if ($choice.archived) { ' [archived]' } else { '' }
        Write-Host ('{0,3}. {1}  {2}{3}' -f ($index + 1), $date, $label, $suffix)
    }
    while ($true) {
        $answer = Read-Host "Choose 1-$($choices.Count), or q to cancel"
        if ([string]::IsNullOrWhiteSpace($answer) -or $answer.Trim() -eq 'q') { return }
        $number = 0
        if ([int]::TryParse($answer.Trim(), [ref]$number) -and $number -ge 1 -and $number -le $choices.Count) {
            $Session = $choices[$number - 1].id
            break
        }
        Write-Host 'Please enter a number from the list, or q.'
    }
}
if ($Session) {
    if (-not $From) { $From = $Profile }
    $resolveArgs = @($agentHistoryTool, 'resolve-codex', '--source', $From, '--session', $Session)
    if ($From -ne $Profile) { $resolveArgs += '--originals-only' }
    $result = & $pythonExe @resolveArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the selected conversation.' }
    $record = $result | ConvertFrom-Json
    if ($From -eq $Profile) {
        $launchArgs += @('resume', $record.id)
    } else {
        $importArgs = @($agentHistoryTool, 'import-codex', $record.id, '--source', $From, '--to', $Profile)
        if ($PrepareOnly) { $importArgs += '--dry-run' }
        $importJson = & $pythonExe @importArgs
        if ($LASTEXITCODE -ne 0) { throw 'Could not import the native conversation.' }
        $imported = $importJson | ConvertFrom-Json
        $launchArgs += @('resume', $imported.session)
        Write-Host "Native conversation: $($imported.session) [$($imported.status)]"
    }
}
$launchArgs += $CodexArgs
if ($PrepareOnly) {
    [pscustomobject]@{Connection=$Profile; Home=$target; Arguments=$launchArgs}
    return
}
$configContent = (Get-Content -LiteralPath $profileConfig -Raw -Encoding UTF8) + "`r`n`r`n" + (Get-Content -LiteralPath $common -Raw -Encoding UTF8)
if ($configContent -match '(?m)^\s*(model|base_url)\s*=\s*"[^"\r\n]*CHANGE_ME_') {
    throw "Connection setup is incomplete. Edit model and base_url in $profileConfig before launching."
}
[IO.File]::WriteAllText((Join-Path $target 'config.toml'), $configContent, $utf8)
if ($entry.sync_history -and -not $NoSync -and (Test-Path -LiteralPath (Join-Path $target 'state_5.sqlite'))) {
    & $pythonExe $agentHistoryTool sync-codex --to $Profile --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Some conversations could not be synced. Run this connection with -SyncHistory for details.'
    }
}
$codexCommand = (Get-Command codex -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if ($entry.launch_args -contains 'code_mode_host') {
    if (-not (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $codexCommand) 'codex-code-mode-host.exe'))) { throw 'Missing code mode host beside Codex.' }
}
$previousHome = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'Process')
try {
    $env:CODEX_HOME = $target
    if ($entry.key_env -and -not [Environment]::GetEnvironmentVariable($entry.key_env, 'Process')) {
        $savedKey = [Environment]::GetEnvironmentVariable($entry.key_env, 'User')
        if ($savedKey) { [Environment]::SetEnvironmentVariable($entry.key_env, $savedKey, 'Process') }
        $savedKey = $null
    }
    if ($entry.key_env -and -not [Environment]::GetEnvironmentVariable($entry.key_env, 'Process')) {
        $secureKey = Read-Host "$($entry.key_env)" -AsSecureString
        $credential = New-Object System.Management.Automation.PSCredential('provider', $secureKey)
        [Environment]::SetEnvironmentVariable($entry.key_env, $credential.GetNetworkCredential().Password, 'Process')
    }
    Write-Host "Connection: $Profile | CODEX_HOME: $target"
    & $codexCommand @launchArgs
} finally {
    [Environment]::SetEnvironmentVariable('CODEX_HOME', $previousHome, 'Process')
}
