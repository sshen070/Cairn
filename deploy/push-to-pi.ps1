# PowerShell wrapper for deploy/push-to-pi.sh
#
#     .\deploy\push-to-pi.ps1              # defaults to pi@192.168.0.222
#     .\deploy\push-to-pi.ps1 jay          # different user, default host
#     .\deploy\push-to-pi.ps1 jay@10.0.0.5 # both
#
# Running the .sh directly from PowerShell does NOT work: Windows associates
# .sh with git-bash.exe, which opens a *separate* window, runs the script there,
# prompts for the password in that window, and closes it when done -- so nothing
# appears in your terminal and there is no way to type the password. This
# wrapper invokes bash in the current console instead, where the prompt works.

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = ''
)

$ErrorActionPreference = 'Stop'

$bash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bash) {
    Write-Error @'
bash not found on PATH. It ships with Git for Windows, usually at
    C:\Program Files\Git\usr\bin\bash.exe
Install Git for Windows, or add that directory to PATH.
'@
    exit 1
}

$script = Join-Path $PSScriptRoot 'push-to-pi.sh'
if (-not (Test-Path $script)) {
    Write-Error "cannot find $script"
    exit 1
}

# Run from the repo root so the script's relative paths resolve.
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if ([string]::IsNullOrWhiteSpace($Target)) {
        & $bash.Source 'deploy/push-to-pi.sh'
    } else {
        & $bash.Source 'deploy/push-to-pi.sh' $Target
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
