[CmdletBinding()]
param([switch]$RepeatEach10)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'install-browser-runner.ps1')
$python = 'C:\Users\balbomush\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path $python)) { throw "BGT-001 infrastructure failure: bundled Python is unavailable: $python" }
$tests = @('tests.test_ui_auth.PlaywrightBearerAuthBrowserTests', 'tests.test_ui_auth.ResponsiveLayoutBrowserTests', 'tests.test_web_ui.WebUiTests.test_v2fly_catalog_update_browser_race_keeps_controls_locked_and_never_reports_false_success', 'tests.test_web_ui.WebUiTests.test_wbg_browser_bootstrap_gate_is_atomic_generic_and_race_safe', 'tests.test_web_ui.WebUiTests.test_wbg_browser_timeout_fails_current_attempt_then_retry_reaches_ready')
$count = if ($RepeatEach10) { 10 } else { 1 }
for ($run = 1; $run -le $count; $run++) { & $python -B -m unittest @tests; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
