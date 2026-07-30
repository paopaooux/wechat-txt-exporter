@echo off
setlocal
cd /d "%~dp0"

fltmc >nul 2>nul
if errorlevel 1 (
  if not "%~1"=="" (
    echo [ERROR] Reading the Weixin database key requires administrator permission.
    echo Open an administrator terminal and run this command again.
    pause
    exit /b 1
  )
  echo Requesting administrator permission to read the local Weixin process...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)

set "PYTHON_EXE="
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; assert sys.version_info >= (3, 13); print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"

if not defined PYTHON_EXE (
  echo [ERROR] Python 3.13 or newer is not installed.
  echo Install Python 3.13+ from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 goto :failed
)

.venv\Scripts\python.exe -c "import importlib.util, wechat_txt_exporter, sqlcipher3, faster_whisper; assert importlib.util.find_spec('pysilk') or importlib.util.find_spec('silk') or importlib.util.find_spec('pilk')" >nul 2>nul
if errorlevel 1 (
  echo Installing database and voice transcription dependencies...
  .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo silk-python installation failed. Trying the pilk decoder fallback...
    .venv\Scripts\python.exe -m pip install --disable-pip-version-check -e . pytest faster-whisper pilk
    if errorlevel 1 goto :failed
  )
)

if "%~1"=="" (
  start "" ".venv\Scripts\pythonw.exe" -m wechat_txt_exporter.gui
  exit /b 0
)

.venv\Scripts\python.exe -m wechat_txt_exporter %*
set "WX_EXPORT_EXIT=%ERRORLEVEL%"
echo.
if "%WX_EXPORT_EXIT%"=="0" echo Export completed successfully.
if "%WX_EXPORT_EXIT%"=="2" echo Export completed with some conversation errors.
pause
exit /b %WX_EXPORT_EXIT%

:failed
echo [ERROR] Setup failed. See the error output above.
pause
exit /b 1
