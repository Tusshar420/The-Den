# Removes The Den from this Windows computer. Started by Uninstall-The-Den.bat.
# It asks before deleting your own data (tasks, projects, logins, notes).

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'TheDen\app')
)

$ErrorActionPreference = 'Stop'
$TaskName = 'The Den'
$Base = Split-Path -Parent $InstallDir
$Interactive = [Environment]::UserInteractive -and -not $env:DEN_INSTALL_YES

# What belongs to the person (the same list as KEEP_NAMES / KEEP_DIRS in launcher.py).
$KeepFiles = @('queue.db', 'queue.db-wal', 'queue.db-shm', 'accounts.json', 'login.json', 'api_keys.json', 'restart_held.json',
               'restart_requested.json', 'den.env', 'den-keys.json', 'mcp-config.local.json')
$KeepDirs = @('workspace', 'vault', 'backups', 'releases', '.den-update')

function Say([string]$Text) { Write-Host $Text }
function Good([string]$Text) { Write-Host "  OK  $Text" -ForegroundColor Green }

function Ask-YesNo([string]$Question, [bool]$Default) {
    if (-not $Interactive) { return $Default }
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = (Read-Host "$Question $hint").Trim().ToLower()
    if (-not $a) { return $Default }
    return ($a -eq 'y' -or $a -eq 'yes')
}

Write-Host ''
Write-Host '  Removing The Den' -ForegroundColor Cyan
if (-not (Test-Path $InstallDir)) { Say "  I can't find The Den in $InstallDir. Nothing to remove there." }

Say ''
Say '  Step 1 of 4: stopping The Den'
if (Test-Path $InstallDir) { Set-Content -Path (Join-Path $InstallDir '.den-stop') -Value 'stop' }
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Good 'Stopped'

Say ''
Say '  Step 2 of 4: removing the start-at-sign-in task and the shortcuts'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$links = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'The Den.url'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\The Den.url'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\The Den.lnk')
)
foreach ($l in $links) { Remove-Item $l -Force -ErrorAction SilentlyContinue }
Good 'Removed'

Say ''
Say '  Step 3 of 4: your data'
Say '  Your data is everything you made in The Den: tasks, projects, notes, files and logins.'
$deleteData = $false
if (Test-Path $InstallDir) {
    $deleteData = Ask-YesNo '  Delete your data too? (Choose No to keep it, for example to install The Den again later.)' $false
}

Say ''
Say '  Step 4 of 4: removing the program files'
if (Test-Path $InstallDir) {
    if ($deleteData) {
        Remove-Item $InstallDir -Recurse -Force
        Remove-Item (Join-Path $Base 'logs') -Recurse -Force -ErrorAction SilentlyContinue
        Good 'Everything was removed, including your data'
    } else {
        # Delete everything except the person's own files and folders.
        Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object {
            -not ($_.PSIsContainer -and $KeepDirs -contains $_.Name) -and -not (-not $_.PSIsContainer -and $KeepFiles -contains $_.Name)
        } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
        Good "The program was removed. Your data is still in $InstallDir"
        Say '  (Delete that folder yourself whenever you no longer want it.)'
    }
}
Say ''
Say '  The Den has been removed. Python, Git and Claude Code were left alone, because other programs may use them.'
Say ''
