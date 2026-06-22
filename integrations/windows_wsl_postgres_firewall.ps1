param(
    [string]$RuleName = "PostgreSQL 5432 from WSL",
    [int]$Port = 5432
)

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator."
}

function Get-CidrFromAddress {
    param(
        [Parameter(Mandatory = $true)][string]$IpAddress,
        [Parameter(Mandatory = $true)][int]$PrefixLength
    )

    $addressBytes = [System.Net.IPAddress]::Parse($IpAddress).GetAddressBytes()
    $networkBytes = New-Object byte[] 4
    $remainingBits = $PrefixLength

    for ($index = 0; $index -lt 4; $index++) {
        if ($remainingBits -ge 8) {
            $maskByte = 255
            $remainingBits -= 8
        }
        elseif ($remainingBits -le 0) {
            $maskByte = 0
        }
        else {
            $maskByte = [int](256 - [math]::Pow(2, 8 - $remainingBits))
            $remainingBits = 0
        }

        $networkBytes[$index] = [byte]($addressBytes[$index] -band $maskByte)
    }

    $network = [System.Net.IPAddress]::new($networkBytes).ToString()
    return "$network/$PrefixLength"
}

$wslAddress = Get-NetIPAddress |
    Where-Object {
        $_.AddressFamily -eq "IPv4" -and
        $_.InterfaceAlias -like "vEthernet (WSL*"
    } |
    Sort-Object SkipAsSource |
    Select-Object -First 1

if (-not $wslAddress) {
    throw "Unable to find the WSL Hyper-V interface."
}

$remoteSubnet = Get-CidrFromAddress -IpAddress $wslAddress.IPAddress -PrefixLength $wslAddress.PrefixLength

if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $remoteSubnet | Out-Null
}
else {
    Set-NetFirewallRule -DisplayName $RuleName -Enabled True | Out-Null
    Set-NetFirewallAddressFilter -AssociatedNetFirewallRule (Get-NetFirewallRule -DisplayName $RuleName) -RemoteAddress $remoteSubnet
}

Write-Output "Firewall rule ready: $RuleName"
Write-Output "Remote subnet: $remoteSubnet"
