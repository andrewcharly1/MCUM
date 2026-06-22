[CmdletBinding()]
param(
    [string] $ArgsFile,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $BridgeArgs
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bridgeScript = Join-Path $scriptDir "openclaw_bridge.py"

if ($ArgsFile) {
    $raw = Get-Content -LiteralPath $ArgsFile -Raw -Encoding UTF8
    if ($raw) {
        $BridgeArgs = @()
        foreach ($item in (ConvertFrom-Json -InputObject $raw)) {
            $BridgeArgs += [string] $item
        }
    }
}

$pythonExe = $null
$pythonArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
    $pythonArgs = @("-3.10")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
} else {
    throw "Python launcher not found. Install Python 3.10+ on Windows."
}

& $pythonExe @pythonArgs $bridgeScript @BridgeArgs
exit $LASTEXITCODE
