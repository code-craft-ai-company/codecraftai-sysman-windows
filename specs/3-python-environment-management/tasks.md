# Implementation Plan — Windows Python Environment Management

Checked items (`[x]`) are implemented and covered by tests.

## Module

- [x] 1. Create `ccai.sysman.windows.pythonenvironmentmanagement`
- [x] 2. Define `Windows`-prefixed aliases of the shared types
- [x] 3. Define `WindowsPythonEnvManagerConfig` (powershell_executable,
  winget_executable, prerequisite_packages)
- [x] 4. Implement `_build_layout` → Windows layout bound to the PowerShell executable
- [x] 5. Implement `ensure_prerequisites` delegating to `WindowsPackageManager`

## Tests

- [x] 6. `test_layout_is_windows_and_uses_configured_powershell`
- [x] 7. `test_environment_exists_uses_powershell_test_path`
- [x] 8. `test_remove_environment_uses_powershell_remove_item`
- [x] 9. `test_ensure_prerequisites_installs_default_winget_package`
- [x] 10. `test_ensure_prerequisites_honours_override_config_and_winget_executable`
- [x] 11. `test_ensure_prerequisites_stops_at_first_failure_and_dry_run`
- [x] 12. `test_plain_base_config_is_accepted`
- [x] 13. `test_elevation_blocked_surfaces_detail_token`
- [x] 14. `test_deploy_end_to_end_on_windows_argv`

## Follow-ups

- [ ] 15. Microsoft Store CPython detection (no `py` launcher) with an automatic
  `base_interpreter` fallback
- [ ] 16. Long-path (`\\?\`) handling for environments deeper than 260 characters
- [ ] 17. Live-host verification over WinRM and OpenSSH on Windows Server 2022
