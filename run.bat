@echo off
rem Launch PC Cleaner without a console window.
cd /d "%~dp0"
start "" pythonw -m pccleaner
