@echo off
setlocal
rem ===================================================================
rem  MFT Search one-click launcher bootstrap.
rem
rem  Pure ASCII on purpose: non-ASCII text inside a .bat gets mangled by
rem  the console code page. All user-facing text lives in launch.py (UTF-8).
rem ===================================================================

where py >nul 2>nul
if %errorlevel%==0 goto :use_py
where python >nul 2>nul
if %errorlevel%==0 goto :use_python

echo [x] Python 3 was not found on PATH.
echo     Install Python 3.13+ from https://www.python.org/ and try again.
pause
exit /b 1

:use_py
py -3 "%~dp0launch.py" %*
if %errorlevel% neq 0 pause
exit /b %errorlevel%

:use_python
python "%~dp0launch.py" %*
if %errorlevel% neq 0 pause
exit /b %errorlevel%
