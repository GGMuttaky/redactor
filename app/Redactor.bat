@echo off
setlocal
rem Starts Redactor and opens it in the default browser. Close this window to quit.
rem First run: creates a private Python environment in .venv and installs the requirements.
rem .venv\setup-ok holds a copy of requirements.txt; when that file changes (an update), setup runs again.
cd /d "%~dp0"

if exist ".venv\setup-ok" (
  fc /b requirements.txt ".venv\setup-ok" >nul 2>&1 && goto :run
)

echo Setting up Redactor. This happens once, needs Python 3.12, 3.13 or 3.14 and an internet
echo connection, and downloads about 100 MB.
echo.
if exist ".venv" rmdir /s /q ".venv"
if exist ".venv" goto :inuse

set "PY="
rem The packages Redactor needs are published for Python 3.12 to 3.14.
for %%V in (3.12 3.13 3.14) do if not defined PY py -%%V -c "" >nul 2>&1 && set "PY=py -%%V"
if not defined PY python -c "import sys; sys.exit(not (3, 12) <= sys.version_info[:2] <= (3, 14))" >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython

%PY% -m venv .venv || goto :venvfailed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :pipfailed
copy /y requirements.txt ".venv\setup-ok" >nul
echo.
echo Setup finished.

:run
".venv\Scripts\python.exe" -m redactor %*
if errorlevel 1 (
  echo.
  echo Redactor stopped because of an error. The message above, and the log file
  echo %LOCALAPPDATA%\Redactor\redactor.log, say what went wrong.
  pause
)
goto :eof

:nopython
echo Python 3.12, 3.13 or 3.14 was not found.
echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
pause
exit /b 1

:inuse
echo Could not remove the old .venv folder. Close any running Redactor window and try again.
pause
exit /b 1

:venvfailed
echo Could not create the Python environment in "%CD%\.venv".
echo Check that this folder is not read-only, then run this again.
pause
exit /b 1

:pipfailed
rmdir /s /q ".venv" >nul 2>&1
echo.
echo Setup failed while installing the requirements. The most common causes:
echo  - No internet connection. Connect and run this again.
echo  - The folder path is too long for Windows ^("path too long" or "No such file or directory" above^).
echo    Move the Redactor folder somewhere short, such as C:\Redactor, and run this again.
pause
exit /b 1
