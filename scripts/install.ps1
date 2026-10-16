param(
    [switch]$Prerequisites,
    [switch]$Schedule,
    [switch]$DryRun,
    [string]$Source = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = 'Stop'
function Find-CompatiblePython {
    $savedManagerInstall = [Environment]::GetEnvironmentVariable('PYTHON_MANAGER_AUTOMATIC_INSTALL', 'Process')
    $savedAllowInstall = [Environment]::GetEnvironmentVariable('PYLAUNCHER_ALLOW_INSTALL', 'Process')
    $savedAlwaysInstall = [Environment]::GetEnvironmentVariable('PYLAUNCHER_ALWAYS_INSTALL', 'Process')
    try {
    # Probing a launcher must not download a runtime or open the Store.
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
    [Environment]::SetEnvironmentVariable('PYLAUNCHER_ALLOW_INSTALL', $null, 'Process')
    [Environment]::SetEnvironmentVariable('PYLAUNCHER_ALWAYS_INSTALL', $null, 'Process')
    $candidates = @(
        @{ Name = 'py'; Prefix = @('-3') },
        @{ Name = (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\py.exe'); Prefix = @('-3') },
        @{ Name = 'python'; Prefix = @() },
        @{ Name = 'python3'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $executable = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $executable) { continue }
        # Skip Store aliases, which can open an unexpected installation window.
        if ($executable.Source -match '\\Microsoft\\WindowsApps\\python(?:3)?\.exe$') { continue }
        $prefix = $candidate.Prefix
        & $executable.Source @prefix -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' *> $null
        if ($LASTEXITCODE -eq 0) { return @{ Executable = $executable.Source; Prefix = $prefix } }
    }
    return $null
    } finally {
        [Environment]::SetEnvironmentVariable('PYTHON_MANAGER_AUTOMATIC_INSTALL', $savedManagerInstall, 'Process')
        [Environment]::SetEnvironmentVariable('PYLAUNCHER_ALLOW_INSTALL', $savedAllowInstall, 'Process')
        [Environment]::SetEnvironmentVariable('PYLAUNCHER_ALWAYS_INSTALL', $savedAlwaysInstall, 'Process')
    }
}
$pythonCommand = Find-CompatiblePython
if (-not $pythonCommand) {
    Write-Host 'Python 3.11+ is required. Official instructions: https://docs.python.org/3/using/windows.html'
    Write-Host 'Install the Python manager: winget install 9NQ7512CXL7T -e --accept-package-agreements --disable-interactivity'
    Write-Host 'Then install a runtime: py install 3.13'
    if (-not $Prerequisites) { exit 1 }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'WinGet is unavailable; use the official Python installer.' }
    & winget install 9NQ7512CXL7T -e --accept-package-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $manager = Get-Command py -ErrorAction SilentlyContinue
    if (-not $manager) {
        $managerPath = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\py.exe'
        if (Test-Path -LiteralPath $managerPath) { $manager = @{ Source = $managerPath } }
    }
    if (-not $manager) { throw 'Python manager installed. Open a new terminal and run: py install 3.13' }
    & $manager.Source install 3.13
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $pythonCommand = Find-CompatiblePython
    if (-not $pythonCommand) { throw 'Open a new terminal after installing Python, then run this installer again.' }
}
$installArguments = @('-B', (Join-Path $PSScriptRoot 'install.py'), '--source', $Source)
if ($Schedule) { $installArguments += '--schedule' }
if ($DryRun) { $installArguments += '--dry-run' }
if ($Prerequisites) { $installArguments += '--prerequisites' }
$pythonPrefix = $pythonCommand.Prefix
& $pythonCommand.Executable @pythonPrefix @installArguments
exit $LASTEXITCODE
