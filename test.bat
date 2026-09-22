@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

python -m compileall -q src tools
if errorlevel 1 exit /b 1

python -m unittest ^
  tests.alignment_diagnostics_test ^
  tests.alignment_policy_test ^
  tests.anchor_history_test ^
  tests.history_delete_test ^
  tests.restyle_versions_test ^
  tests.webui_policy_test ^
  tests.setup_menu_test ^
  tests.concert_splitter_test
if errorlevel 1 exit /b 1

echo Release unit tests passed.
