@echo off
rem ===================================================================
rem  Taipower load-curve relay crawler -- backup machine (Windows) launcher.
rem  Called by the scheduled tasks registered by register_backup_task.ps1.
rem
rem  !! THIS FILE MUST STAY PURE ASCII -- comments included. !!
rem  cmd.exe reads a .bat/.cmd as raw bytes in the console OEM codepage
rem  (cp932 on this box). UTF-8 Chinese in a rem line decodes into byte
rem  pairs that swallow the line ending, cmd then tries to execute half a
rem  comment and the whole script dies with "is not recognized as an
rem  internal or external command". Hit once on 2026-09-12.
rem  The Chinese explanation of this deployment lives in docs/DEPLOY.md.
rem
rem  Why the wrapper exists at all (details in docs/DEPLOY.md):
rem    1. PYTHONUTF8=1 is mandatory: console is cp932, so Python's stdout
rem       would raise UnicodeEncodeError on the first Chinese log line and
rem       kill the run -- which looks like "the crawler broke" but is only
rem       "it could not print".
rem    2. Task Scheduler's working directory is not guaranteed to be the
rem       project root, and config.yml paths + data/ archiving resolve
rem       relative to it.
rem    3. Task Scheduler has no StandardOutPath like launchd, so we tee the
rem       log ourselves.
rem
rem  Mode comes from config/config.yml (mode: backup on this machine).
rem  Add --backup to force the standby check regardless of config.
rem ===================================================================
setlocal
set PYTHONUTF8=1
set PROJ=%~dp0..
cd /d "%PROJ%"

if not exist "data" mkdir "data"
set LOG=%PROJ%\data\backup-run.log

echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
"%PROJ%\venv\Scripts\python.exe" scripts\run_once.py >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ----- exit=%RC% ----->> "%LOG%"

rem standby exits 0, same as a successful run. Non-zero means a real failure.
exit /b %RC%
