@echo off
chcp 936 >nul 2>nul
setlocal

rem ============================================================
rem  PDF Translator - launch the graphical interface
rem  Just double-click this file.
rem ============================================================

set "HERE=%~dp0"
set "PYW="

if exist "%HERE%.venv\Scripts\pythonw.exe" set "PYW=%HERE%.venv\Scripts\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not defined PYW if exist "%ProgramFiles%\Python313\pythonw.exe" set "PYW=%ProgramFiles%\Python313\pythonw.exe"
if not defined PYW if exist "%ProgramFiles%\Python312\pythonw.exe" set "PYW=%ProgramFiles%\Python312\pythonw.exe"
if not defined PYW if exist "%ProgramFiles%\Python311\pythonw.exe" set "PYW=%ProgramFiles%\Python311\pythonw.exe"
if not defined PYW if exist "C:\Python312\pythonw.exe" set "PYW=C:\Python312\pythonw.exe"
if not defined PYW if exist "C:\Python311\pythonw.exe" set "PYW=C:\Python311\pythonw.exe"

if not defined PYW (
    echo.
    echo  [ERROR] Python not found.
    echo.
    echo  Please double-click "°²×°ÒÀÀµ.bat" first.
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "%HERE%run_gui.pyw"
exit /b 0
