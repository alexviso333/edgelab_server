@echo off
cd /d "%~dp0"
echo Parando el edgelab que estaba abierto en el puerto 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /f /t /pid %%a >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq edgelab" /im cmd.exe >nul 2>&1
timeout /t 2 /nobreak >nul
call "%~dp0abrir_en_windows.bat"
