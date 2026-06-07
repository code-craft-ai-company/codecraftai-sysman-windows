# Implementation Plan

- [x] 1. Wire the module skeleton
  - Create `ccai/sysman/windows/mailtransportmanagement.py`
    with the module docstring documenting the Windows MTA gap and
    the production-guidance recommendation (run real MTA on Linux
    or macOS).
  - Define module-level constants:
    `_DEFAULT_POWERSHELL`, `_PS_NOPROFILE`, `_PS_NONINTERACTIVE`,
    `_PS_COMMAND`, `_DEFAULT_IIS_SMTP_QUEUE_DIR`,
    `_DETAIL_NO_MTA`, `_DETAIL_THIRD_PARTY_NOT_IMPL`,
    `_DETAIL_IIS_SMTP_NO_ALIASES`.
  - Alias `WindowsMtaActionStatus = ActionStatus`.
  - _Requirements: 9, 12_

- [x] 2. Define the `WindowsMtaKind` enum
  - Three values: `NONE`, `IIS_SMTP`, `THIRD_PARTY`.
  - Document each value's coverage in the docstring.
  - _Requirements: 1, 2, 12_

- [x] 3. Define data models
  - `WindowsMtaManagerConfig(FeatureConfig)` with `mta_kind`,
    `iis_smtp_queue_dir`, `powershell_executable`,
    `iis_smtp_service_name`, `iis_smtp_localhost`.
  - `WindowsMtaActionResult(FeatureActionResult)` with `setting`
    and `local`.
  - `WindowsMtaConfig` with `mta_kind`, the named directive
    fields, `extras`, and `detail`.
  - `WindowsMtaAliasInfo` with `local` and `remote`.
  - `WindowsMtaQueueStatus` with the four standard fields.
  - `WindowsMtaQueryError(Exception)`.
  - _Requirements: 1, 2, 3, 4, 5, 7, 12_

- [x] 4. Implement read verbs
  - `get_mta_config()` — branch on `mta_kind`; synthetic shapes
    for `NONE` / `THIRD_PARTY`; `Get-WmiObject` PowerShell pipeline
    + `_parse_iissmtp_get_output` for `IIS_SMTP`.
  - `list_aliases()` — always return `[]`.
  - `get_queue_status()` — branch on `mta_kind`; zeroed status for
    `NONE` / `THIRD_PARTY`; `Get-ChildItem` + parse for
    `IIS_SMTP`.
  - All three read verbs MUST pass `elevated=False`.
  - _Requirements: 1, 5, 7, 10_

- [x] 5. Implement `set_mta_config`
  - Branch on `mta_kind`.
  - `NONE`: FAILED with `_DETAIL_NO_MTA`, no runner call.
  - `THIRD_PARTY`: FAILED with `_DETAIL_THIRD_PARTY_NOT_IMPL`.
  - `IIS_SMTP`: Honour dry-run; build PowerShell pipeline via
    `_build_iissmtp_set_script`; dispatch with `elevated`; wrap
    result.
  - _Requirements: 2, 9, 10, 11, 12_

- [x] 6. Implement alias write verbs
  - `add_alias()` — always FAILED with the appropriate stable
    detail string (`_DETAIL_NO_MTA`,
    `_DETAIL_THIRD_PARTY_NOT_IMPL`, or
    `_DETAIL_IIS_SMTP_NO_ALIASES`).
  - `remove_alias()` — same surface.
  - Neither verb invokes the runner on any path.
  - _Requirements: 3, 4, 9_

- [x] 7. Implement operational write verbs
  - `test_mail_delivery()` — branch on `mta_kind`; for `IIS_SMTP`
    build a `Send-MailMessage` PowerShell pipeline via
    `_build_send_mailmessage_script` and dispatch with elevation.
  - `flush_queue()` — branch on `mta_kind`; for `IIS_SMTP`
    dispatch `Restart-Service -Name '<service>' -Force`.
  - Both verbs honour dry-run on `IIS_SMTP`; never skip on any
    kind.
  - _Requirements: 6, 8, 9, 10, 11_

- [x] 8. Implement script builders + parsers
  - `_build_iissmtp_get_script` — `Get-WmiObject IIsSmtpServerSetting`
    + `ConvertTo-Json -Compress`.
  - `_build_iissmtp_set_script` — property-setter chain +
    `$s.Put_()`.
  - `_build_iissmtp_queue_status_script` — `Get-ChildItem`
    + `Measure-Object`.
  - `_build_send_mailmessage_script` — `Send-MailMessage` with
    every required parameter.
  - `_ps_escape` — single-quote escape.
  - `_parse_iissmtp_get_output` — JSON -> config.
  - `_parse_iissmtp_queue_status_output` — JSON -> queue status.
  - `_extract_json_value` — tolerant key/value matcher.
  - _Requirements: 1, 2, 6, 7, 8_

- [x] 9. Author requirements doc
  - Twelve requirements covering every read / write verb, the
    three `mta_kind` branches, stable detail strings, elevation,
    dry-run, and the documented caveats.
  - _Requirements: -_

- [x] 10. Author design doc
  - Architecture diagram, configuration, data models, MTA mapping
    with cross-OS concept-mapping table that highlights the
    Windows gap, key method designs, idempotency model, dry-run /
    elevation policy, correctness properties, testing strategy,
    seams.
  - _Requirements: -_

- [x] 11. Author test module (>= 14 tests)
  - `test_get_mta_config_synthetic_when_mta_kind_none`.
  - `test_get_mta_config_parses_iis_smtp_output`.
  - `test_set_mta_config_failed_when_mta_kind_none`.
  - `test_set_mta_config_invokes_powershell_for_iis_smtp`.
  - `test_set_mta_config_skipped_per_setting_when_match_iis_smtp`.
  - `test_set_mta_config_dry_run_short_circuits`.
  - `test_add_alias_failed_when_mta_kind_none`.
  - `test_add_alias_failed_iis_smtp_unsupported`.
  - `test_remove_alias_failed_when_mta_kind_none`.
  - `test_list_aliases_returns_empty_list`.
  - `test_test_mail_delivery_failed_when_mta_kind_none`.
  - `test_test_mail_delivery_invokes_send_mailmessage_for_iis_smtp`.
  - `test_test_mail_delivery_dry_run_short_circuits`.
  - `test_get_queue_status_zero_when_mta_kind_none`.
  - `test_get_queue_status_parses_iis_smtp_count`.
  - `test_flush_queue_failed_when_mta_kind_none`.
  - `test_flush_queue_restarts_smtpsvc_for_iis_smtp`.
  - `test_writes_elevate`.
  - `test_reads_never_elevate`.
  - `test_failed_command_surfaces_stderr`.
  - `test_writes_fail_when_mta_kind_none` (covers every write
    verb in one sweep).
  - `test_send_mail_works_with_iis_smtp_kind` (regression for the
    happy-path IIS_SMTP probe).
  - Module-level parser sanity tests on `_parse_iissmtp_get_output`,
    `_parse_iissmtp_queue_status_output`, and the script
    builders.
  - _Requirements: 1-12_

- [ ] 12. Follow-ups (not part of this wave)
  - Auto-detect installed Windows MTAs and choose `mta_kind`
    transparently.  The pilot requires explicit operator opt-in.
  - Multi-virtual-server IIS SMTP support — today the WMI query
    uses `Select-Object -First 1`.
  - Native command suites for hMailServer / MailEnable to lift
    `THIRD_PARTY` out of placeholder status.
  - Capture + parse `Send-MailMessage` output to confirm queue
    acceptance instead of relying on exit code only.
  - Per-property idempotency probe on `set_mta_config` IIS SMTP
    path (read first, gate write client-side).
  - Cross-feature integration: emit a `MaintenanceReport` row per
    write verb once the Windows package adopts the report bus.
