@echo off
where gradle >nul 2>nul
if %errorlevel% neq 0 (
  echo gradle command not found in PATH
  exit /b 1
)
gradle %*
