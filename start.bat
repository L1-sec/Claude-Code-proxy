@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\user\Desktop\output\session_menu.ps1" -Tool Claude
pause
