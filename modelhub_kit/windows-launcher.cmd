@echo off
rem modelhub-kit:launcher:v1
"__BINARY__" --cli %*
if not "%ERRORLEVEL%"=="86" exit /b %ERRORLEVEL%
rem Close the native executable before the removal adapter unlinks it.
setlocal DisableDelayedExpansion
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "MODELHUB_KIT_ADAPTER="
for /f "tokens=1,* delims=|" %%A in ('""__BINARY__" --find-python-adapter"') do (
  set "MODELHUB_KIT_ADAPTER_KIND=%%A"
  set "MODELHUB_KIT_ADAPTER=%%B"
)
if not defined MODELHUB_KIT_ADAPTER goto missing_python
if "%MODELHUB_KIT_ADAPTER_KIND%"=="manager" goto python_manager
"%MODELHUB_KIT_ADAPTER%" -I -B "__COMPAT__" %*
exit /b %ERRORLEVEL%
:python_manager
"%MODELHUB_KIT_ADAPTER%" -3 -I -B "__COMPAT__" %*
exit /b %ERRORLEVEL%
:missing_python
echo {"status":"queued","reason":"python-adapter-unavailable"}
exit /b 75
