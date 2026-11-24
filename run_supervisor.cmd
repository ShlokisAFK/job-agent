@echo off
cd /d "%~dp0"
python -m supervisor.supervisor >> workspace\supervisor.log 2>&1
