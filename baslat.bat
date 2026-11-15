@echo off
cd /d "%~dp0"
if exist "%~dp0dist\VideoIndirici.exe" (
    start "" "%~dp0dist\VideoIndirici.exe"
) else (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop.py"
)
