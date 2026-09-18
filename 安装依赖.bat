@echo off
chcp 936 >nul 2>nul
setlocal

rem ============================================================
rem  PDF Translator - install / check runtime dependencies
rem  Just double-click this file.
rem ============================================================

set "HERE=%~dp0"
set "PY="

if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%ProgramFiles%\Python313\python.exe" set "PY=%ProgramFiles%\Python313\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python311\python.exe" set "PY=%ProgramFiles%\Python311\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"

if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | find /i "WindowsApps" >nul
        if errorlevel 1 if not defined PY set "PY=%%P"
    )
)

if not defined PY (
    echo.
    echo  [ERROR] Python not found.
    echo.
    echo  Please install Python 3.10 or newer from:
    echo      https://www.python.org/downloads/
    echo  During installation, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

echo  Using Python: %PY%
"%PY%" --version

echo.
echo  Installing dependencies ^(PyMuPDF, requests^) ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%HERE%requirements.txt"

if errorlevel 1 (
    echo.
    echo  [ERROR] Installation failed. Try a faster mirror:
    echo      "%PY%" -m pip install -r "%HERE%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   Done. Now you can:
echo     * double-click  "∆Ù∂ØΩÁ√Ê.bat"      to open the GUI
echo     * run           "translate-pdf.bat --help"  for the CLI
echo  ============================================================
echo.
pause
exit /b 0
