# Windows-friendly demo: the full app with NO kernel changes (tc commands are only recorded)
# and simulated telemetry. Dashboard: http://localhost:8000
$root = $PSScriptRoot
$env:ORCH_DRY_RUN = "1"
$env:PYTHONPATH = Join-Path $root "core-orchestrator"
& (Join-Path $root ".venv\Scripts\python.exe") -m uvicorn app.main:app --host 127.0.0.1 --port 8000
