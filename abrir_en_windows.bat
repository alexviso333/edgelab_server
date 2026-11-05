@echo off
setlocal
title edgelab
REM Abre edgelab en tu PC con Windows. Necesitas Python 3.11 o superior (python.org, marca Add to PATH).
cd /d "%~dp0"
echo.
echo  edgelab - arrancando en %CD%
echo.

if exist ".venv\Scripts\python.exe" goto :venv_ok
echo Preparando el entorno la primera vez, tarda unos minutos...
python -m venv .venv
if errorlevel 1 goto :no_python
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fallo
:venv_ok

if exist "data\edgelab.sqlite" goto :db_ok
if /i "%~1"=="real" goto :real
echo Creando datos de DEMOSTRACION (mundo simulado) para que veas la web...
".venv\Scripts\python.exe" -m edgelab.cli demo
goto :db_ok
:real
echo Descargando historico real. Puede tardar horas; puedes cerrar y volver a abrir.
".venv\Scripts\python.exe" -m edgelab.cli backfill --seasons 4
".venv\Scripts\python.exe" -m edgelab.cli lab
:db_ok

echo  Abriendo la web y el planificador. NO cierres esta ventana mientras la uses.
echo  Si algo falla, el detalle queda en data\arranque.log
echo.
".venv\Scripts\python.exe" -u -m edgelab.cli app --host 127.0.0.1 --port 8000
if errorlevel 1 goto :fallo
goto :fin

:no_python
echo.
echo No encuentro Python. Instalalo desde python.org marcando "Add python.exe to PATH".
goto :fin

:fallo
echo.
echo Algo ha fallado. Mira el mensaje de arriba y data\arranque.log
:fin
echo.
pause
