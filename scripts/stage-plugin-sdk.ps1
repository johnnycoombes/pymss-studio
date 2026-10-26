param(
    [Parameter(Mandatory = $true)]
    [string]$StagePath
)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "../src-tauri/resources/plugin-sdk"
$destination = Join-Path $StagePath "python/plugin-sdk"
New-Item -ItemType Directory -Force -Path $destination | Out-Null
foreach ($name in @("pymss-sdk.js", "pymss-sdk.css")) {
    $target = Join-Path $destination $name
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination $target -Force
    if ((Get-Item -LiteralPath $target).Length -eq 0) {
        throw "Staged plugin SDK file is empty: $name"
    }
}
