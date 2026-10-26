# Run once in an elevated PowerShell window. Only local-subnet clients are allowed.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as administrator, then run this script again.'
}

$ruleName = 'Guikerong-LAN-TCP-8000'
$rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($rule) {
    Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow `
        -Profile Any -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet | Out-Null
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName 'Guikerong LAN TCP 8000' `
        -Enabled True -Direction Inbound -Action Allow -Profile Any `
        -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet | Out-Null
}
Write-Output 'Allowed TCP 8000 from the local subnet. Windows Firewall remains enabled.'
