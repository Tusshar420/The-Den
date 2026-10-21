# Installs (or updates) The Den on Windows. Started by Install-The-Den.bat.
#
# No administrator rights are needed: everything goes into your own user folder.
# Safe to run again: it then updates the program files and keeps your tasks, logins and settings.
#
# Optional settings (the .bat passes them through):
#   -InstallDir  where The Den goes (default: %LOCALAPPDATA%\TheDen\app)
#   -Port        the port it listens on (default 8787)
#   -Network     let other computers on your network reach this Den (needed for sharing); default is this computer only
#   -NoOpen      don't open the browser at the end
# For unattended runs: DEN_LOGIN_USER and DEN_LOGIN_PASSWORD set the first login; DEN_INSTALL_YES=1 stops all questions.

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'TheDen\app'),
    [int]$Port = 8787,
    [switch]$Network,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # makes downloads much faster in Windows PowerShell
$TaskName = 'The Den'
$TotalSteps = 7
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)        # installer\windows -> the unzipped folder
$LogDir = Join-Path $env:LOCALAPPDATA 'TheDen\logs'
$LogFile = Join-Path $LogDir 'install.log'
$Interactive = [Environment]::UserInteractive -and -not $env:DEN_INSTALL_YES

# Files and folders that belong to the person, never replaced or removed by an install or update.
# Keep this list the same as KEEP_NAMES / KEEP_DIRS in launcher.py.
$KeepFiles = @('queue.db*', 'accounts.json', 'login.json', 'api_keys.json', 'restart_held.json',
               'restart_requested.json', 'den.env', 'den-keys.json', 'mcp-config.local.json', '.den-stop', '.den-service.lock')
$KeepDirs = @('.venv', '.approval-venv', '.den-update', '.git', 'workspace', 'vault', 'backups', 'releases')

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Add-Content -Path $LogFile -Value ("`n=== Install started " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " ===")

function Say([string]$Text) { Write-Host $Text }
function Step([int]$N, [string]$Text) { Write-Host ''; Write-Host "Step $N of ${TotalSteps}: $Text" -ForegroundColor Cyan }
function Good([string]$Text) { Write-Host "  OK  $Text" -ForegroundColor Green }
function Note([string]$Text) { Write-Host "      $Text" }
function Warn([string]$Text) { Write-Host "  !   $Text" -ForegroundColor Yellow }

function Fail([string]$Text) {
    Write-Host ''
    Write-Host "  The install stopped: $Text" -ForegroundColor Red
    Write-Host "  Nothing is broken - you can run Install-The-Den.bat again once this is fixed." -ForegroundColor Red
    Write-Host "  A detailed log is here: $LogFile"
    exit 1
}

# Starts a program, waits, and gives back its exit code and what it printed. (Plain "&" mixes a program's
# warnings into PowerShell errors, so this keeps them apart.)
function Run([string]$Exe, [string[]]$Arguments) {
    $out = [System.IO.Path]::GetTempFileName()
    $err = [System.IO.Path]::GetTempFileName()
    try {
        $line = ($Arguments | ForEach-Object { if ($_ -match '[\s"]' -or $_ -eq '') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
        if (-not $line) { $line = ' ' }
        $p = Start-Process -FilePath $Exe -ArgumentList $line -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $o = [System.IO.File]::ReadAllText($out)
        $e = [System.IO.File]::ReadAllText($err)
        [System.IO.File]::AppendAllText($LogFile, "`n> $Exe $line`n$o$e`n(exit code $($p.ExitCode))`n")
        return [pscustomobject]@{ Code = $p.ExitCode; Out = $o; Err = $e }
    } finally {
        Remove-Item $out, $err -ErrorAction SilentlyContinue
    }
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Have-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }

function Winget-Install([string]$Id, [string[]]$Extra) {
    $wargs = @('install', '--id', $Id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements') + $Extra
    $r = Run (Get-Command winget).Source $wargs
    return ($r.Code -eq 0)
}

function Ask-YesNo([string]$Question, [bool]$Default) {
    if (-not $Interactive) { return $Default }
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = (Read-Host "$Question $hint").Trim().ToLower()
    if (-not $a) { return $Default }
    return ($a -eq 'y' -or $a -eq 'yes')
}

function Read-EnvFile([string]$Path) {
    $h = @{}
    if (Test-Path $Path) {
        foreach ($l in [System.IO.File]::ReadAllLines($Path)) {
            $t = $l.Trim().TrimStart([char]0xFEFF)
            if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
                $i = $t.IndexOf('=')
                $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
            }
        }
    }
    return $h
}

function Write-EnvFile([string]$Path, $Values) {
    $lines = @('# The Den settings. Read by den_service.py when it starts. Keep this file private: it holds your API key.')
    foreach ($k in ($Values.Keys | Sort-Object)) { $lines += "$k=$($Values[$k])" }
    [System.IO.File]::WriteAllText($Path, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))
    $me = "$env:USERDOMAIN\$env:USERNAME"
    Run 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', "${me}:F") | Out-Null      # only you can read it
}

function Wait-Health([int]$Seconds) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Stop-Den {
    # Ask the supervisor to stop (the .den-stop file), stop the task, and end anything still running from this folder.
    if (Test-Path $InstallDir) { Set-Content -Path (Join-Path $InstallDir '.den-stop') -Value 'stop' }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

Write-Host ''
Write-Host '  ==========================' -ForegroundColor Cyan
Write-Host '    Installing The Den' -ForegroundColor Cyan
Write-Host '  ==========================' -ForegroundColor Cyan
Say '  This takes about 5 to 10 minutes. You can leave this window open and wait.'
Say '  Nothing here needs administrator rights.'

if (-not (Test-Path (Join-Path $Root 'main.py'))) {
    Fail "I can't find The Den's files next to this installer. Unzip the whole download first, then double-click Install-The-Den.bat from the unzipped folder."
}
$Updating = Test-Path (Join-Path $InstallDir 'queue.db')
if ($Updating) { Say '  The Den is already installed here, so this will update it. Your tasks, logins and settings are kept.' }

# ------------------------------------------------------------------ 1. Python
Step 1 'Checking for Python (the language The Den is written in)'
function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $candidates += , @($py.Source, '-3') }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { $candidates += , @($p.Source) }
    $dirs = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue
    foreach ($d in $dirs) { $exe = Join-Path $d.FullName 'python.exe'; if (Test-Path $exe) { $candidates += , @($exe) } }
    foreach ($c in $candidates) {
        try {
            $exe = $c[0]
            $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            $r = Run $exe ($pre + @('-c', 'import sys; print(sys.executable); print(sys.version_info[0] * 100 + sys.version_info[1])'))
            if ($r.Code -eq 0) {
                $lines = $r.Out -split "`r?`n" | Where-Object { $_ }
                if ($lines.Count -ge 2 -and [int]$lines[1] -ge 311) { return $lines[0] }
            }
        } catch { }
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Note 'Python 3.11 or newer was not found.'
    if (Have-Winget) {
        Note 'Installing Python for you (this can take a few minutes)...'
        if (Winget-Install 'Python.Python.3.12' @('--scope', 'user')) { Refresh-Path }
        $Python = Find-Python
    }
}
if (-not $Python) {
    Fail 'Python 3.11 or newer is needed and I could not install it myself. Please install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH" on the first screen), then run Install-The-Den.bat again.'
}
Good "Python is ready ($Python)"

# ------------------------------------------------------------------ 2. Git
Step 2 'Checking for Git (Claude Code on Windows needs it)'
function Find-GitBash {
    $c = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe")
    $g = Get-Command git -ErrorAction SilentlyContinue
    if ($g) { $c += (Join-Path (Split-Path -Parent (Split-Path -Parent $g.Source)) 'bin\bash.exe') }
    foreach ($p in $c) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}
$GitBash = Find-GitBash
if (-not (Get-Command git -ErrorAction SilentlyContinue) -or -not $GitBash) {
    Note 'Git for Windows was not found.'
    if (Have-Winget) {
        Note 'Installing Git for you (Windows may ask you to allow it)...'
        if (-not (Winget-Install 'Git.Git' @('--scope', 'user'))) { Winget-Install 'Git.Git' @() | Out-Null }
        Refresh-Path
        $GitBash = Find-GitBash
    }
}
if (-not $GitBash) {
    Fail 'Git for Windows is needed and I could not install it myself. Please install it from https://git-scm.com/download/win (the default choices are fine), then run Install-The-Den.bat again.'
}
Good 'Git is ready'

# ------------------------------------------------------------------ 3. Claude Code
Step 3 'Checking for Claude Code (the AI that does the work)'
function Find-Claude {
    $c = Get-Command claude -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:APPDATA\npm\claude.cmd")) { if (Test-Path $p) { return $p } }
    return $null
}
$Claude = Find-Claude
if (-not $Claude) {
    Note 'Claude Code was not found. Installing it with its official installer...'
    $env:CLAUDE_CODE_GIT_BASH_PATH = $GitBash
    $r = Run 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', 'irm https://claude.ai/install.ps1 | iex')
    Refresh-Path
    $Claude = Find-Claude
}
if (-not $Claude) {
    Fail 'Claude Code could not be installed automatically. Open https://claude.ai/download (or https://docs.claude.com/en/docs/claude-code/setup), follow the Windows steps there, then run Install-The-Den.bat again.'
}
Good "Claude Code is ready ($Claude)"
$ClaudeSignedIn = $false
try {
    $env:CLAUDE_CODE_GIT_BASH_PATH = $GitBash
    $r = Run $Claude @('auth', 'status')
    $ClaudeSignedIn = ($r.Code -eq 0 -and $r.Out -match '"loggedIn"\s*:\s*true')
} catch { }
if ($ClaudeSignedIn) { Good 'You are already signed in to Claude' }
else { Note 'You are not signed in to Claude yet (this is asked again at the end).' }

# ------------------------------------------------------------------ 4. Files
Step 4 "Copying The Den's files to $InstallDir"
if ($Updating -or (Test-Path (Join-Path $InstallDir 'main.py'))) { Stop-Den }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$xd = @('installer', '__pycache__') + $KeepDirs + @((Join-Path $InstallDir 'static\project-images'))
# robocopy without /MIR or /PURGE never deletes anything in the install folder. 0-7 mean "worked".
$rc = Run 'robocopy.exe' (@($Root, $InstallDir, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:2', '/W:2', '/XD') + $xd + @('/XF') + $KeepFiles + @('README-FIRST.txt', 'Install-The-Den.bat', 'Uninstall-The-Den.bat'))
if ($rc.Code -ge 8) { Fail "Copying the files failed (code $($rc.Code))." }
Remove-Item (Join-Path $InstallDir '.den-stop') -ErrorAction SilentlyContinue
Good 'Files are in place'

# ------------------------------------------------------------------ 5. Packages
Step 5 'Setting up The Den (installing its Python packages; this is the slow part)'
$VenvPy = Join-Path $InstallDir '.venv\Scripts\python.exe'
$VenvPyw = Join-Path $InstallDir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $VenvPy)) {
    $r = Run $Python @('-m', 'venv', (Join-Path $InstallDir '.venv'))
    if ($r.Code -ne 0) { Fail 'Could not create The Den''s Python environment.' }
}
$r = Run $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip')
$r = Run $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $InstallDir 'requirements.txt'))
if ($r.Code -ne 0) { Fail 'Installing the Python packages failed. Check your internet connection and run the installer again.' }
# the helper that asks you before Claude runs something risky
$r = Run $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', 'mcp<2')
if ($r.Code -ne 0) { Fail 'Installing the approval helper failed. Check your internet connection and run the installer again.' }
Good 'Packages are installed'

# ------------------------------------------------------------------ 6. Settings
Step 6 'Writing your settings'
$EnvPath = Join-Path $InstallDir 'den.env'
$settings = Read-EnvFile $EnvPath
if (-not $settings['API_KEY']) {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $settings['API_KEY'] = (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
    Good 'Made a private key for this Den (it stays on this computer)'
}
if ($Network) { $settings['HOST'] = '0.0.0.0' }
elseif (-not $settings['HOST']) {
    Say ''
    Say '  Should other computers on your network be able to reach this Den?'
    Say '  Say No unless you plan to share it with a colleague. You can change this later.'
    $settings['HOST'] = if (Ask-YesNo '  Let other computers connect?' $false) { '0.0.0.0' } else { '127.0.0.1' }
}
$settings['PORT'] = "$Port"
$settings['CLAUDE_BIN'] = $Claude
$settings['CLAUDE_CODE_GIT_BASH_PATH'] = $GitBash
$settings['PYTHONUTF8'] = '1'
Write-EnvFile $EnvPath $settings
if ($settings['HOST'] -eq '127.0.0.1') { Good 'The Den will only be reachable from this computer' }
else { Good 'The Den will be reachable from your network (Windows may ask to allow it - choose Allow on private networks)' }

# ------------------------------------------------------------------ 7. Start
Step 7 'Starting The Den and making shortcuts'
$me = "$env:USERDOMAIN\$env:USERNAME"
$service = Join-Path $InstallDir 'den_service.py'
$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $VenvPyw -Argument ('"' + $service + '"') -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Principal $principal `
        -Description 'Keeps The Den running in the background.' -Force | Out-Null
    $registered = $true
    Good 'The Den will now start by itself whenever you sign in to Windows'
} catch {
    Warn "Windows would not let me add the start-at-sign-in task ($($_.Exception.Message))."
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    try {
        $sh = New-Object -ComObject WScript.Shell
        $lnk = $sh.CreateShortcut((Join-Path $startup 'The Den.lnk'))
        $lnk.TargetPath = $VenvPyw
        $lnk.Arguments = '"' + $service + '"'
        $lnk.WorkingDirectory = $InstallDir
        $lnk.WindowStyle = 7
        $lnk.Save()
        Good 'Used a Startup shortcut instead, so it still starts when you sign in'
    } catch { Warn 'Could not set up starting at sign-in. You can start The Den from the "The Den" shortcut.' }
}
Remove-Item (Join-Path $InstallDir '.den-stop') -ErrorAction SilentlyContinue
if ($registered) { Start-ScheduledTask -TaskName $TaskName }
else { Start-Process -FilePath $VenvPyw -ArgumentList ('"' + $service + '"') -WorkingDirectory $InstallDir -WindowStyle Hidden }
Note 'Waiting for The Den to wake up (the first start can take a minute)...'
if (-not (Wait-Health 150)) {
    Fail "The Den did not start. Its log is in $env:LOCALAPPDATA\TheDen\logs\stdout.log - please send that file to whoever gave you The Den."
}
Good 'The Den is running'

$url = "http://127.0.0.1:$Port/dashboard"
$shortcutText = "[InternetShortcut]`r`nURL=$url`r`n"
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($dir in @($desktop, $startMenu)) {
    try { [System.IO.File]::WriteAllText((Join-Path $dir 'The Den.url'), $shortcutText) } catch { Warn "Could not make a shortcut in $dir" }
}
Good 'Shortcuts named "The Den" are on your Desktop and in the Start menu'

# Keep a copy of the uninstaller outside the program folder, so it can remove that folder.
$home2 = Split-Path -Parent $InstallDir
try {
    Copy-Item (Join-Path $PSScriptRoot 'Uninstall-The-Den.bat') $home2 -Force
    Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') $home2 -Force
} catch { }

# The first login. The dashboard would otherwise ask for the long private key, which is awkward.
$headers = @{ 'X-API-Key' = $settings['API_KEY'] }
$needLogin = $false
try { $needLogin = -not (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/auth/state" -Headers $headers -TimeoutSec 10).configured } catch { }
$madeLogin = $false
if ($needLogin) {
    Say ''
    Say '  Now choose how you will sign in to The Den. Pick any name and a password of 8 or more characters.'
    for ($try = 0; $try -lt 3 -and -not $madeLogin; $try++) {
        $user = $env:DEN_LOGIN_USER; $plain = $env:DEN_LOGIN_PASSWORD
        if (-not $user -or -not $plain) {
            if (-not $Interactive) { break }
            $user = (Read-Host '  Your name for signing in').Trim()
            if (-not $user) { Note 'Skipped. The Den will ask you to create one when it opens.'; break }
            $sec = Read-Host '  Your password' -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        }
        try {
            $body = @{ username = $user; password = $plain; remember = $true } | ConvertTo-Json
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/auth/setup" -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
            $madeLogin = $true
            Good "Your login is ready. Sign in as: $user"
        } catch {
            $why = "that did not work"
            try { $why = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch { }
            Warn "Could not save it: $why"
            $env:DEN_LOGIN_USER = $null; $env:DEN_LOGIN_PASSWORD = $null
        }
    }
    $plain = $null
}

# Sign in to Claude if that is still to do
if (-not $ClaudeSignedIn -and $Interactive) {
    Say ''
    if (Ask-YesNo '  Sign in to Claude now? (a browser window opens)' $true) {
        try { $env:CLAUDE_CODE_GIT_BASH_PATH = $GitBash; Start-Process -FilePath $Claude -ArgumentList 'auth login' -Wait -NoNewWindow } catch { Warn 'That did not start. The steps below show another way.' }
        try { $r = Run $Claude @('auth', 'status'); $ClaudeSignedIn = ($r.Code -eq 0 -and $r.Out -match '"loggedIn"\s*:\s*true') } catch { }
    }
}

Write-Host ''
Write-Host '  ==========================' -ForegroundColor Green
Write-Host '    The Den is installed' -ForegroundColor Green
Write-Host '  ==========================' -ForegroundColor Green
Say ''
Say '  What to do next:'
Say '   1. The Den opens in your browser. If it does not, double-click "The Den" on your Desktop.'
if ($madeLogin) { Say '   2. Sign in with the name and password you just chose.' }
else { Say '   2. It will ask you to create a login. It also asks once for your private key: it is the API_KEY line in the file' ; Say "      $EnvPath  (open it with Notepad)." }
if (-not $ClaudeSignedIn) {
    Say '   3. Sign in to Claude, or the AI can not work: open the Start menu, type  cmd  and press Enter,'
    Say '      type  claude  and press Enter, then follow the steps on screen. Do this once.'
}
Say ''
Say '  The Den runs quietly in the background and starts by itself each time you sign in to Windows.'
Say "  Its files:  $InstallDir"
Say "  Its logs:   $LogDir"
Say "  To remove it later, double-click Uninstall-The-Den.bat in $home2"
Say ''
if (-not $NoOpen) { Start-Process $url }
