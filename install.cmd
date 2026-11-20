@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [!] 找不到 python。
  echo     装 Python 3.9+ 时务必勾选 "Add python.exe to PATH"：
  echo     https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

python "%~dp0tools\install_skill.py" %*
set RC=%errorlevel%
if not "%RC%"=="0" pause
exit /b %RC%
