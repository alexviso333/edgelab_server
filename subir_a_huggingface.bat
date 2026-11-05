@echo off
cd /d "%~dp0"
title Subir edgelab a Hugging Face
if not exist ".venv\Scripts\python.exe" (echo Abre primero edgelab una vez con abrir_en_windows.bat & pause & exit /b)
".venv\Scripts\python.exe" -m pip install -q huggingface_hub
".venv\Scripts\python.exe" deploy\huggingface\subir.py %1
pause
