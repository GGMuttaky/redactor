@echo off
rem Starts Redactor and opens it in the default browser. Close this window to quit.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Redactor. This needs Python 3.12 and an internet connection once.
  py -3.12 -m venv .venv || python -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :failed
)
".venv\Scripts\python.exe" -m redactor %*
goto :eof

:nopython
echo Python 3.12 was not found. Install it from https://www.python.org/downloads/ and run this again.
pause
goto :eof

:failed
echo Setup failed. Check the internet connection and run this again.
pause
