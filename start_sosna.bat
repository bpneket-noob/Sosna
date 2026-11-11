@echo off
rem Запуск «Сосны» из папки установки (относительные пути — работает на любой машине)
set "SOSNA_DIR=%~dp0"
start "" "%SOSNA_DIR%runtime\pythonw.exe" "%SOSNA_DIR%main.py"
