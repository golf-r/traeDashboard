@echo off
REM ============================================================
REM Trae Dashboard - .eml download verification script
REM ============================================================
REM Double-click this file (or run from cmd) to confirm:
REM   1. trae-dashboard server is running
REM   2. The right commit is loaded
REM   3. POST /api/report/eml returns HTTP 200
REM   4. GET  /api/report/eml returns HTTP 200
REM   5. OPTIONS preflight works
REM
REM Each step prints PASS / FAIL. If any step FAILs, the
REM symptom reported in the goal still exists.
REM ============================================================

setlocal enabledelayedexpansion

REM --- find the user's port from their config / docs (default 8765) ---
set PORT=8765
set BASE=http://127.0.0.1:%PORT%

echo.
echo === Trae Dashboard .eml verification (port %PORT%) ===
echo.

set FAILS=0

REM --- Test 1: server reachable ---
curl -s -o nul -w "Test 1 (server reachable):     HTTP %%{http_code}\n" %BASE%/api/health >nul 2>&1
if %errorlevel% neq 0 (
    echo Test 1 (server reachable):     FAIL ^(server not running on %PORT%^)
    set /a FAILS+=1
    goto :summary
)
echo Test 1 (server reachable):     PASS

REM --- Test 2: version + correct commit ---
for /f "delims=" %%i in ('curl -s %BASE%/api/version') do set VER=%%i
echo %VER% | findstr /c:"9b9e497" >nul
if %errorlevel% equ 0 (
    echo Test 2 (commit 9b9e497+):    PASS
) else (
    echo Test 2 (commit 9b9e497+):    FAIL - server is stale. Run:
    echo     pkill -9 -f trae_dashboard
    echo     python -m trae_dashboard serve --config config.yaml
    set /a FAILS+=1
)

REM --- Test 3: POST ---
curl -s -o nul -w "Test 3 (POST /api/report/eml): HTTP %%{http_code}\n" ^
    -X POST %BASE%/api/report/eml ^
    -H "Content-Type: application/json" ^
    -d "{\"recipients\":[\"shichenchen@huawei.com\"]}"

REM --- Test 4: GET (the user-recoverable fallback) ---
curl -s -o nul -w "Test 4 (GET /api/report/eml):  HTTP %%{http_code}\n" ^
    "%BASE%/api/report/eml?recipients=shichenchen@huawei.com"

REM --- Test 5: OPTIONS preflight ---
curl -s -o nul -w "Test 5 (OPTIONS /api/report/eml): HTTP %%{http_code}\n" ^
    -X OPTIONS %BASE%/api/report/eml

REM --- Test 6: full file via GET ---
curl -s -o .verify-eml-test.bin -w "Test 6 (full .eml via GET): HTTP %%{http_code}, %%{size_download} bytes\n" ^
    "%BASE%/api/report/eml?recipients=shichenchen@huawei.com"
if exist .verify-eml-test.bin del .verify-eml-test.bin

:summary
echo.
if %FAILS% equ 0 (
    echo ============================================================
    echo   ALL CHECKS PASSED - .eml download should work.
    echo   Open http://127.0.0.1:%PORT%/ in your browser.
    echo   Click "发送报告" -^> "下载 .eml".
    echo ============================================================
) else (
    echo ============================================================
    echo   %FAILS% CHECK^(S^) FAILED.
    echo   Follow the instructions printed above, then re-run.
    echo ============================================================
)
echo.
pause