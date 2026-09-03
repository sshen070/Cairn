# Open (or check, or close) the SSH tunnel to the Cairn server.
#
#     .\deploy\tunnel.ps1              # open it; blocks until you Ctrl+C
#     .\deploy\tunnel.ps1 -Status      # is it up, and does the server answer?
#     .\deploy\tunnel.ps1 -Stop        # close it
#     .\deploy\tunnel.ps1 -Background  # detach (needs key auth; see below)
#
# The tunnel forwards localhost:8823 on this machine to 127.0.0.1:8823 on the
# Pi. The server binds to loopback there, so this is the only route in -- which
# means SSH is doing the authentication and the loopback bind is the access
# control. Nothing on the LAN can reach the server directly.

[CmdletBinding()]
param(
    [string]$Target = 'pi@192.168.0.222',
    [int]$Port = 8823,
    [switch]$Status,
    [switch]$Stop,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
$Url = "http://127.0.0.1:$Port/v1/health"

function Test-Listening {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-TunnelProcesses {
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "-L\s*$Port`:" }
}

function Test-Server {
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing
        return ($r.Content | ConvertFrom-Json)
    } catch { return $null }
}

# --- status ---------------------------------------------------------------

if ($Status) {
    if (Test-Listening) {
        Write-Host "tunnel  localhost:$Port is listening" -ForegroundColor Green
    } else {
        Write-Host "tunnel  localhost:$Port is NOT listening" -ForegroundColor Yellow
    }
    $h = Test-Server
    if ($h) {
        Write-Host "server  $($h.status) - $($h.devices) device(s), $($h.objects) object(s)" -ForegroundColor Green
    } else {
        Write-Host "server  no answer on $Url" -ForegroundColor Yellow
        Write-Host "        the tunnel may be up with nothing listening on the Pi;"
        Write-Host "        check there with:  systemctl status cairn-server"
    }
    $procs = @(Get-TunnelProcesses)
    if ($procs) { Write-Host "ssh     PID $($procs.ProcessId -join ', ')" }
    exit 0
}

# --- stop -----------------------------------------------------------------

if ($Stop) {
    $procs = @(Get-TunnelProcesses)
    if (-not $procs) { Write-Host "no tunnel process found for port $Port"; exit 0 }
    foreach ($p in $procs) {
        Write-Host "stopping ssh PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force
    }
    exit 0
}

# --- open -----------------------------------------------------------------

if (Test-Listening) {
    Write-Host "port $Port is already in use." -ForegroundColor Yellow
    $h = Test-Server
    if ($h) {
        Write-Host "the Cairn server already answers through it - nothing to do." -ForegroundColor Green
        exit 0
    }
    Write-Host "but nothing answers on $Url. Close the old one first:  .\deploy\tunnel.ps1 -Stop" -ForegroundColor Yellow
    exit 1
}

# ServerAliveInterval keeps a idle tunnel from being dropped by the router;
# ExitOnForwardFailure makes ssh fail loudly instead of connecting with no
# forward, which otherwise looks like "the server is down".
$sshArgs = @(
    '-N',
    '-L', "${Port}:127.0.0.1:$Port",
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes',
    $Target
)

# Can we authenticate without a password? Only then is detaching possible --
# a backgrounded ssh has no terminal to prompt on.
$keyWorks = $false
try {
    & ssh -o BatchMode=yes -o ConnectTimeout=8 $Target 'true' 2>$null
    $keyWorks = ($LASTEXITCODE -eq 0)
} catch { $keyWorks = $false }

if ($Background) {
    if (-not $keyWorks) {
        Write-Host "-Background needs key authentication; there is no terminal to type a password into." -ForegroundColor Yellow
        Write-Host "set a key up first:"
        Write-Host "    ssh-copy-id -i ~/.ssh/id_rsa.pub $Target"
        exit 1
    }
    Start-Process -FilePath 'ssh' -ArgumentList $sshArgs -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (Test-Server) {
        Write-Host "tunnel up in the background. Close it with:  .\deploy\tunnel.ps1 -Stop" -ForegroundColor Green
        exit 0
    }
    Write-Host "started, but the server did not answer. Check with -Status." -ForegroundColor Yellow
    exit 1
}

Write-Host "opening tunnel  localhost:$Port  ->  ${Target}:127.0.0.1:$Port"
if (-not $keyWorks) {
    Write-Host "you will be asked for the password (no ssh key installed yet)" -ForegroundColor Yellow
}
Write-Host "leave this window open; Ctrl+C closes the tunnel."
Write-Host ""
& ssh @sshArgs
