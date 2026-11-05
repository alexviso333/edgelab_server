@echo off
cd /d "%~dp0"
echo inicio %date% %time% > data\diag.log
where python >> data\diag.log 2>&1
.venv\Scripts\python --version >> data\diag.log 2>&1
.venv\Scripts\python -c "import edgelab.cli, edgelab.worker, edgelab.api.app; print('imports ok')" >> data\diag.log 2>&1
netstat -ano | findstr :8000 >> data\diag.log 2>&1
tasklist | findstr /i python >> data\diag.log 2>&1
echo lanzando >> data\diag.log
.venv\Scripts\python -m edgelab.cli app --host 127.0.0.1 --port 8000 --no-browser > data\app_run.log 2>&1
