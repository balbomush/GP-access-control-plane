[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent (Split-Path -Parent $repo)
$packageDir = Join-Path $repo 'tests\browser'
$lockPath = Join-Path $packageDir 'toolchain.lock.json'
try { $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json } catch { throw "BGT-001 infrastructure failure: invalid toolchain lock: $_" }
if ($lock.schema -ne 1 -or -not $lock.runtimeId -or -not $lock.node -or -not $lock.playwright -or -not $lock.browser) { throw 'BGT-001 infrastructure failure: incomplete toolchain lock' }
$runtime = Join-Path $workspace (Join-Path 'runtime\bgt-001' $lock.runtimeId)
$nodeVersion = [string]$lock.node.version
$nodeUrl = [string]$lock.node.url
$nodeSha256 = [string]$lock.node.sha256
$nodeExeSha256 = [string]$lock.node.nodeExeSha256
$playwrightVersion = [string]$lock.playwright.version
$playwrightPackage = [string]$lock.playwright.package
$chromiumRevision = [string]$lock.browser.revision
$nodeRuntime = Join-Path $runtime 'node'
$projectRuntime = Join-Path $runtime 'playwright-project'
$nodeArchive = Join-Path $nodeRuntime "node-v$nodeVersion-win-x64.zip"
New-Item -ItemType Directory -Force -Path $nodeRuntime, $projectRuntime | Out-Null
if (-not (Test-Path (Join-Path $nodeRuntime 'node.exe')) -or -not (Test-Path (Join-Path $nodeRuntime 'npm.cmd'))) {
  Invoke-WebRequest -Uri $nodeUrl -OutFile $nodeArchive
  $actualSha256 = (Get-FileHash -LiteralPath $nodeArchive -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actualSha256 -ne $nodeSha256) { throw "BGT-001 infrastructure failure: Node SHA-256 mismatch: $actualSha256" }
  Expand-Archive -LiteralPath $nodeArchive -DestinationPath $nodeRuntime -Force
  Get-ChildItem -LiteralPath (Join-Path $nodeRuntime "node-v$nodeVersion-win-x64") -Force | Move-Item -Destination $nodeRuntime -Force
  Remove-Item -LiteralPath (Join-Path $nodeRuntime "node-v$nodeVersion-win-x64") -Force
}
$node = Join-Path $nodeRuntime 'node.exe'
$npm = Join-Path $nodeRuntime 'npm.cmd'
$env:npm_config_cache = Join-Path $runtime 'npm-cache'
$actualNodeSha256 = (Get-FileHash -LiteralPath $node -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualNodeSha256 -ne $nodeExeSha256) { throw "BGT-001 infrastructure failure: existing Node SHA-256 mismatch: $actualNodeSha256" }
Copy-Item (Join-Path $packageDir 'package.json') (Join-Path $projectRuntime 'package.json') -Force
Copy-Item (Join-Path $packageDir 'package-lock.json') (Join-Path $projectRuntime 'package-lock.json') -Force
Push-Location $projectRuntime
try { & $npm ci --ignore-scripts --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw "BGT-001 infrastructure failure: npm ci exited $LASTEXITCODE" } } finally { Pop-Location }
$targetModules = Join-Path $projectRuntime 'node_modules'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtime 'browsers'
& $node (Join-Path $targetModules "$playwrightPackage\cli.js") install chromium
if ($LASTEXITCODE -ne 0) { throw "BGT-001 infrastructure failure: Playwright Chromium install exited $LASTEXITCODE" }
$actualPlaywrightVersion = (Get-Content -LiteralPath (Join-Path $targetModules "$playwrightPackage\package.json") -Raw | ConvertFrom-Json).version
if ($actualPlaywrightVersion -ne $playwrightVersion) { throw "BGT-001 infrastructure failure: Playwright version mismatch: expected $playwrightVersion, actual $actualPlaywrightVersion" }
$chromium = Join-Path $env:PLAYWRIGHT_BROWSERS_PATH "chromium-$chromiumRevision"
if (-not (Test-Path -LiteralPath $chromium -PathType Container)) { throw "BGT-001 infrastructure failure: Chromium revision mismatch: expected $chromiumRevision, missing $chromium" }
Write-Output "BGT-001 runner installed: $runtime"
