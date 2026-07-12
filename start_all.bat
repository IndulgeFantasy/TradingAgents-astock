@echo off
setlocal

REM TradingAgents-Astock launcher
REM Start: Chrome + playwright_service + tradingagents-web
REM Stop: Ctrl+C or close window, all services auto-killed

set "ProjectRoot=E:\PycharmProject\TradingAgents-astock"
set "ChromeExe=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "ChromeProfile=E:\ChromeAutomationProfile"
set "CondaActivate=E:\Anaconda\Scripts\activate.bat"
set "PwEnv=worktrade2"
set "MainEnv=worktrade"

REM Trap Ctrl+C and window close
if "%~1"=="_cleanup" goto :cleanup

REM --- [1/3] Chrome CDP ---
echo [1/3] Starting Chrome (CDP :9222)
if not exist "%ChromeProfile%" mkdir "%ChromeProfile%"
del /f /q "%ChromeProfile%\SingletonLock" 2>nul
del /f /q "%ChromeProfile%\SingletonCookie" 2>nul
del /f /q "%ChromeProfile%\SingletonSocket" 2>nul
start "" "%ChromeExe%" --remote-debugging-port=9222 --user-data-dir="%ChromeProfile%" --no-first-run --no-default-browser-check
echo     Chrome started

REM --- [2/3] playwright_service ---
echo [2/3] Starting playwright_service (port :8765)
start "playwright_service" cmd /c "call "%CondaActivate%" %PwEnv% && cd /d "%ProjectRoot%" && python playwright_service\server.py --port 8765"
echo     playwright_service started

REM --- [3/3] tradingagents-web ---
echo [3/3] Starting tradingagents-web
start "tradingagents-web" cmd /c "call "%CondaActivate%" %MainEnv% && cd /d "%ProjectRoot%" && tradingagents-web"
echo     tradingagents-web started

echo.
echo All services started:
echo   Chrome CDP        : http://127.0.0.1:9222
echo   Playwright Service: http://127.0.0.1:8765/api/health
echo   Web UI            : http://localhost:8501
echo.
echo Close this window to stop all services.
echo ----------------------------------------------------

REM Keep alive - wait until user closes window
pause >nul

:cleanup
echo.
echo Stopping all services...
taskkill /f /im chrome.exe 2>nul
taskkill /f /fi "windowtitle eq playwright_service*" 2>nul
taskkill /f /fi "windowtitle eq tradingagents-web*" 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING" 2^>nul') do taskkill /pid %%a /f /t 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8501 " ^| findstr "LISTENING" 2^>nul') do taskkill /pid %%a /f /t 2>nul
echo All stopped.
endlocal
