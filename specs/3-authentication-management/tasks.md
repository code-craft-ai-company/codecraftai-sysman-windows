# Tasks

## Implementation Plan

### 1. Module Skeleton

- [ ] Create
  `source/packages/automojo/sysman/windows/authenticationmanagement.py`.
- [ ] Add module-level constants for the PowerShell executable,
  PowerShell flags, `secedit.exe`, the LSA registry path, the
  `Authentication Packages` registry name, and temp file paths.
- [ ] Import `ActionStatus`, `BaseFeatureManager`,
  `FeatureActionResult`, `FeatureConfig` from
  `automojo.sysman.windows.featurebase`.

---

### 2. Data Models

- [ ] Alias `WindowsAuthActionStatus = ActionStatus`.
- [ ] Define `WindowsAuthSubsystem` dataclass with `kind` and
  `name`.
- [ ] Define module-level constants `WINDOWS_AUTH_LOGON`,
  `WINDOWS_AUTH_NETWORK`, `WINDOWS_AUTH_INTERACTIVE`,
  `WINDOWS_AUTH_SERVICE`.
- [ ] Define `WindowsAuthEntry` dataclass with `policy_key`,
  `policy_value`, and `section` fields.
- [ ] Define `WindowsAuthConfig`, `WindowsAuthMethodInfo`,
  `WindowsAuthValidationResult` dataclasses.
- [ ] Define `WindowsAuthActionResult` extending
  `FeatureActionResult` with `subsystem`, `method`, `message`.
- [ ] Define `WindowsAuthenticationManagerConfig` extending
  `FeatureConfig` with `powershell_executable`,
  `secedit_executable`, `lsa_path`, `temp_cfg_path`,
  `temp_sdb_path`, `well_known_packages`.

---

### 3. Exceptions

- [ ] Define `WindowsAuthenticationQueryError` with `command` /
  `exit_code` / `stderr` constructor.
- [ ] Define `WindowsAuthenticationConfigError`.
- [ ] Define `WindowsAuthenticationSubsystemNotFoundError`.

---

### 4. Parsers / Renderers

- [ ] Implement `_parse_secedit_inf(text)` returning
  `list[WindowsAuthEntry]` with `section` populated.
- [ ] Implement `_render_secedit_inf(config)` producing an INF
  body with the canonical `[Unicode]` / `[Version]` preamble and
  CRLF line endings.
- [ ] Implement `_parse_lsa_packages_json(text)` returning a
  `list[str]` of currently-enabled package names from the
  `Get-ItemProperty | ConvertTo-Json` output.
- [ ] Implement `_format_multi_string_for_powershell(values)`
  producing the `'pkg1','pkg2','pkg3'` argument literal.

---

### 5. Manager — Read Verbs

- [ ] Implement `get_auth_config(subsystem)`. Run secedit export;
  `Get-Content`; parse INF; filter section.
- [ ] Implement `list_auth_methods()`. Run `Get-ItemProperty`;
  parse JSON; merge with `well_known_packages`.
- [ ] Implement `validate_auth_config(subsystem)`. Export; run
  `secedit /validate`; supplement with parser-driven check.

---

### 6. Manager — Write Verbs

- [ ] Implement `set_auth_config(subsystem, config)`. Read; compare;
  SKIPPED; DRY_RUN; `Set-Content`; `secedit /configure`.
- [ ] Implement `enable_method(name)`. Probe LSA; SKIPPED if
  present; DRY_RUN; `Set-ItemProperty`.
- [ ] Implement `disable_method(name)`. Probe LSA; SKIPPED if
  absent; DRY_RUN; `Set-ItemProperty`.

---

### 7. Manager — Internal Helpers

- [ ] `_typed_config()` narrows the base config.
- [ ] `_build_powershell_argv(script)` constructs the PowerShell
  argv (mirrors `auditmanagement._build_powershell_argv`).
- [ ] `_resolve_subsystem(subsystem)` returns the INF section name.
- [ ] `_run_secedit_export()` exports the live policy and returns
  the parsed INF body.
- [ ] `_run_get_lsa_packages()` reads the LSA list.
- [ ] `_build_result(...)` composes a result without invoking the
  runner.
- [ ] `_wrap_runner_result(...)` maps a `RunResult` to a
  `WindowsAuthActionResult`.

---

### 8. Tests

- [ ] Create
  `source/testroots/testbase/automojo/tests/sysman/windows/test_authenticationmanagement.py`.
- [ ] Add the `_MockCommandRunner` shape.
- [ ] `test_get_auth_config_parses_secedit_output`.
- [ ] `test_set_auth_config_invokes_secedit_configure`.
- [ ] `test_set_auth_config_skipped_when_match`.
- [ ] `test_set_auth_config_dry_run`.
- [ ] `test_list_auth_methods_parses_get_itemproperty_output`.
- [ ] `test_list_auth_methods_collapses_single_string_multistring`.
- [ ] `test_enable_method_invokes_set_itemproperty`.
- [ ] `test_enable_method_skipped_when_enabled`.
- [ ] `test_disable_method_invokes_set_itemproperty`.
- [ ] `test_disable_method_skipped_when_disabled`.
- [ ] `test_validate_auth_config_returns_result`.
- [ ] `test_writes_elevate`.
- [ ] `test_reads_never_elevate`.
- [ ] `test_failed_command_surfaces_stderr`.
- [ ] `test_secedit_export_imports_correct_format`.
- [ ] `test_inf_render_round_trip_is_stable`.

---

### 9. Quality Bar

- [ ] No emojis anywhere in source / specs / tests.
- [ ] All public types prefixed `Windows`.
- [ ] Sphinx docstrings on every public method.
- [ ] Explicit `return` statements; no compound returns.
- [ ] No mutable default values.
- [ ] Named-parameter passing throughout.
- [ ] Multi-line error messages built via the list / join pattern.
