$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Invoke-Checked($Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with $LASTEXITCODE" }
}
Invoke-Checked py @('-3.13', '-m', 'venv', '.venv')
Invoke-Checked '.\.venv\Scripts\python.exe' @('-m', 'pip', 'install', '-r', 'requirements.txt')
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env' }
$revision = '60fec01b48aac4f3f313adc380da815462979ee7'
if (!(Test-Path 'wallet')) {
    Invoke-Checked git @('clone', 'https://github.com/tagai-dao/tagclaw-wallet.git', 'wallet')
    Invoke-Checked git @('-C', 'wallet', 'checkout', '--detach', $revision)
}
if ((git -C wallet rev-parse HEAD) -ne $revision) { throw 'Unexpected wallet revision' }
$patch = Join-Path $PSScriptRoot 'wallet-explicit-chain.patch'
$walletSource = Get-Content -LiteralPath 'wallet/src/claw.js' -Raw -Encoding UTF8
if (!$walletSource.Contains("function clawSignerConfig(cfg, chain)")) {
    Invoke-Checked git @('-C', 'wallet', 'apply', '--ignore-space-change', $patch)
}
Write-Host 'Python and wallet source ready. Continue wallet initialization in README.md.'
