@echo off
chcp 936 >nul 2>nul
setlocal

rem ============================================================
rem  PDF Translator - command line entry
rem  Usage: translate-pdf.bat file.pdf --api-key sk-xxx
rem         translate-pdf.bat --help
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

rem Fall back to PATH, but skip the useless Microsoft Store stub
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
    echo  Please double-click "°²×°ÒÀÀµ.bat" first, or install Python 3.10+
    echo  from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

"%PY%" "%HERE%run_cli.py" %*
exit /b %ERRORLEVEL%
