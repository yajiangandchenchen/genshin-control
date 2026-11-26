@echo off
setlocal DisableDelayedExpansion
"%~dp0bin\node.exe" "%~dp0scripts\setup.mjs" %*
exit /b %errorlevel%
