# Jarvis one-command setup for Windows.
#
# Paste this into PowerShell (Start menu -> type "PowerShell" -> Enter):
#   irm https://raw.githubusercontent.com/busbymj1986-wq/Jarvis/main/setup.ps1 | iex
#
# It installs Git and Python if they're missing, downloads Jarvis to your user folder,
# asks for your Claude key, creates a "Start Jarvis" shortcut on your Desktop, and starts it.
# Running it again updates Jarvis and keeps your settings and data.

$ErrorActionPreference = "Stop"
$Repo   = "https://github.com/busbymj1986-wq/Jarvis.git"
$Folder = Join-Path $HOME "Jarvis"

function Say($text) { Write-Host "`n>> $text" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Find-Python {
    # Prefer the py launcher; "python" may be the Microsoft Store placeholder.
    if (Have "py") {
        try { & py -3 --version *> $null; if ($LASTEXITCODE -eq 0) { return @("py", "-3") } } catch {}
    }
    if (Have "python") {
        try {
            $v = & python --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) { return @("python") }
        } catch {}
    }
    return $null
}

function Install-WithWinget($id, $name) {
    if (-not (Have "winget")) {
        throw "$name is missing and winget isn't available. Install $name yourself, then run this again."
    }
    Say "Installing $name (a Windows prompt may ask for permission)..."
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
}

# 1. Git and Python ---------------------------------------------------------
if (-not (Have "git")) { Install-WithWinget "Git.Git" "Git" }
$py = Find-Python
if (-not $py) {
    Install-WithWinget "Python.Python.3.12" "Python 3.12"
    $py = Find-Python
    if (-not $py) { throw "Python was installed but isn't on PATH yet. Close PowerShell, open a new one and run this again." }
}
$pyExe  = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)

# 2. Download or update Jarvis ----------------------------------------------
if (Test-Path (Join-Path $Folder ".git")) {
    Say "Updating Jarvis in $Folder..."
    git -C $Folder pull --ff-only
} else {
    Say "Downloading Jarvis to $Folder..."
    git clone $Repo $Folder
}
Set-Location $Folder

# 3. Python environment ------------------------------------------------------
$venvPython = Join-Path $Folder ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Say "Creating the Python environment..."
    & $pyExe @pyArgs -m venv .venv
}
Say "Installing Jarvis's libraries (takes a minute the first time)..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing libraries failed - see the messages above." }

# 4. Settings (.env) ---------------------------------------------------------
$envFile = Join-Path $Folder ".env"
$utf8 = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path $envFile)) {
    $lines = [IO.File]::ReadAllLines((Join-Path $Folder ".env.example"))

    $bytes = New-Object byte[] 30
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $apiKey = ([Convert]::ToBase64String($bytes) -replace "[^A-Za-z0-9]", "")

    Say "Paste your Claude API key (from console.anthropic.com -> API keys) and press Enter."
    Write-Host "   (It won't show on screen. Leave it blank to add it later in $envFile)"
    $secure = Read-Host "Claude key" -AsSecureString
    $claudeKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()

    $lines = $lines | ForEach-Object {
        if ($_ -match "^JARVIS_API_KEY=")      { "JARVIS_API_KEY=$apiKey" }
        elseif ($_ -match "^ANTHROPIC_API_KEY=") { "ANTHROPIC_API_KEY=$claudeKey" }
        else { $_ }
    }
    [IO.File]::WriteAllLines($envFile, $lines, $utf8)

    Write-Host ""
    Write-Host "Your Jarvis password (JARVIS_API_KEY) is:" -ForegroundColor Yellow
    Write-Host "   $apiKey" -ForegroundColor Yellow
    Write-Host "It's saved in $envFile - you'll need it to use the API."
} else {
    Say "Keeping your existing settings in $envFile"
}

# 5. Desktop shortcut --------------------------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$bat = Join-Path $desktop "Start Jarvis.bat"
$batText = "@echo off`r`ntitle Jarvis`r`ncd /d `"$Folder`"`r`nstart `"`" /min cmd /c `"timeout /t 4 >nul & start http://localhost:5000`"`r`n`"$venvPython`" app.py`r`npause`r`n"
[IO.File]::WriteAllText($bat, $batText, (New-Object System.Text.ASCIIEncoding))
Say "Added 'Start Jarvis' to your Desktop. Double-click it any time to start Jarvis."

# 6. Start -------------------------------------------------------------------
Say "Starting Jarvis at http://localhost:5000 - leave this window open. Press Ctrl+C to stop."
Start-Job -ScriptBlock { Start-Sleep -Seconds 4; Start-Process "http://localhost:5000" } | Out-Null
& $venvPython app.py
