param(
    [string]$PythonExe = 'python.exe',
    [string]$Prefix,
    [string]$SharedRoot,
    [switch]$Desktop,
    [switch]$CodexMcp,
    [switch]$ClaudeMcp,
    [switch]$ClaudeHook,
    [switch]$RegisterTasks,
    [switch]$SyncTask,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedPython = (Get-Command $PythonExe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$installArgs = @((Join-Path $repoRoot 'install.py'), '--python', $resolvedPython)
if ($Prefix) { $installArgs += @('--prefix', $Prefix) }
if ($SharedRoot) { $installArgs += @('--shared-root', $SharedRoot) }
if ($Desktop) { $installArgs += '--desktop' }
if ($CodexMcp) { $installArgs += '--codex-mcp' }
if ($ClaudeMcp) { $installArgs += '--claude-mcp' }
if ($ClaudeHook) { $installArgs += '--claude-hook' }
if ($RegisterTasks) { $installArgs += '--collect-task' }
if ($SyncTask) { $installArgs += '--sync-task' }
if ($DryRun) { $installArgs += '--dry-run' }
& $resolvedPython @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. See diagnostics above and docs/first-use.md.' }
