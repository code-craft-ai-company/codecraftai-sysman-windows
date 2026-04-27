# Tasks

This task list maps the Windows localization-management implementation
to the requirements in `requirements.md` and the design in
`design.md`. Every task is exercised by at least one test in
`source/testroots/testbase/automojo/tests/sysman/windows/test_localizationmanagement.py`.

---

## In-scope tasks

### 1. Module skeleton and exports

- [x] Create
      `source/packages/automojo/sysman/windows/localizationmanagement.py`.
- [x] Export `WindowsLocalizationManager`,
      `WindowsLocalizationManagerConfig`,
      `WindowsLocalizationActionStatus`,
      `WindowsLocalizationActionResult`, `WindowsLocaleInfo`,
      `WindowsKeymapInfo`, `WindowsLocalizationQueryError`.
- [x] Subclass `BaseFeatureManager` and set `feature_name =
      "localization-management"`, `result_class =
      WindowsLocalizationActionResult`,
      `status_class = WindowsLocalizationActionStatus`.
- [x] Export the bundled `_IANA_TO_WINDOWS_TZ` mapping table
      (~30 entries).

### 2. Read verbs

- [x] `get_locale()` — `Get-Culture | ConvertTo-Json` plus
      best-effort `Get-WinSystemLocale | ConvertTo-Json`. Raises
      `WindowsLocalizationQueryError` on stderr-bearing non-zero exit.
      (Requirement 1)
- [x] `list_supported_locales()` —
      `[CultureInfo]::GetCultures(AllCultures) | Select Name`.
      (Requirement 3)
- [x] `get_keymap()` — `Get-WinUserLanguageList | Select
      LanguageTag, InputMethodTips`. Accepts the bare-object shape
      `ConvertTo-Json` emits for single-row pipelines.
      (Requirement 4)
- [x] `get_supported_keymaps()` — wraps `get_keymap()`; surfaces
      the deduplicated union of language tags and input-method TIPs.
      (Requirement 6)
- [x] `get_timezone()` — `Get-TimeZone | Select Id, DisplayName`.
      (Requirement 7)
- [x] `get_supported_timezones()` —
      `Get-TimeZone -ListAvailable | Select Id`. (Requirement 9)
- [x] None of the read paths pass `elevated=True`.
      (Requirement 10)

### 3. Write verbs

- [x] `set_locale(locale)` — `Set-Culture` per-user; optional
      `Set-WinSystemLocale` second-phase dispatch when
      `apply_system_wide=True`; result reports
      `requires_reboot=True` when the system-wide step ran.
      (Requirement 2)
- [x] `set_keymap(keymap)` — `Set-WinUserLanguageList -LanguageList
      '<keymap>' -Force`. (Requirement 5)
- [x] `set_timezone(tz)` — IANA<->Windows resolution via
      `_IANA_TO_WINDOWS_TZ`; ``unknown_timezone_id`` synthetic
      FAILED on no match; idempotency probe via `Get-TimeZone`.
      (Requirement 8)
- [x] Each write honours `config.elevated` on the dispatch step
      and never elevates on the probe step. (Requirement 10)

### 4. Result-shape population

- [x] Populate `result.setting` to `"locale"`, `"keymap"`, or
      `"timezone"` on every returned write result.
- [x] Populate `result.previous_value` from the read probe.
- [x] Set `result.requires_reboot=True` only on the
      `Set-WinSystemLocale` SUCCESS path (or its dry-run echo).
- [x] Carry `message="unknown_timezone_id"` on the synthetic FAILED
      path for unmapped timezone inputs.

### 5. Module-level helpers

- [x] `_ps_escape_single_quoted(value)`.
- [x] `_normalize_to_list(parsed)` — bare-object normaliser.
- [x] `_parse_json_object(text)` — single-object parser.
- [x] `_parse_culture_payload(text)` — `Get-Culture.Name`.
- [x] `_parse_system_locale_payload(text)` —
      `Get-WinSystemLocale.Name`.
- [x] `_parse_timezone_payload(text)` — `Get-TimeZone.Id`.
- [x] `_parse_user_language_list(text)` — array-of-records.
- [x] `_parse_culture_list(text)` — culture name list.
- [x] `_parse_timezone_list(text)` — Windows-format ID list.
- [x] `_derive_country_from_culture(language)` — pure derivation.
- [x] `_resolve_timezone_id(requested, available_windows_ids=None)`
      — IANA-or-Windows-form resolution.

### 6. PowerShell argv hygiene

- [x] Every PowerShell dispatch uses
      `["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
      "<script>"]`.
- [x] `WindowsLocalizationManagerConfig.powershell_executable`
      lets callers swap in `pwsh.exe` for PowerShell 7+.

### 7. Tests

- [x] `test_get_locale_parses_get_culture_output`
- [x] `test_get_keymap_parses_get_winuserlanguagelist_output`
- [x] `test_get_keymap_handles_single_entry_bare_object`
- [x] `test_get_timezone_parses_get_timezone_output`
- [x] `test_list_supported_locales_returns_list`
- [x] `test_get_supported_timezones_parses_list`
- [x] `test_get_supported_keymaps_returns_list`
- [x] `test_set_locale_invokes_set_culture`
- [x] `test_set_locale_skipped_when_match`
- [x] `test_set_locale_dry_run_short_circuits`
- [x] `test_set_locale_apply_system_wide_marks_reboot`
- [x] `test_set_keymap_invokes_set_winuserlanguagelist`
- [x] `test_set_keymap_skipped_when_match`
- [x] `test_set_timezone_invokes_set_timezone_with_windows_id`
- [x] `test_set_timezone_resolves_iana_to_windows_id` (REQUIRED
      by the cross-OS-divergent test plan)
- [x] `test_set_timezone_unknown_iana_returns_failed`
- [x] `test_set_timezone_skipped_when_match`
- [x] `test_set_timezone_dry_run_short_circuits`
- [x] `test_writes_elevate_per_policy`
- [x] `test_reads_never_elevate`
- [x] `test_failed_command_surfaces_stderr`
- [x] `test_get_locale_raises_on_get_culture_failure`

Total: 22 tests; the cross-OS-divergent contract requires at least
14, and the Wave 3.1 plan additionally requires
`test_set_timezone_resolves_iana_to_windows_id`.

### 8. Spec deliverables

- [x] `specs/3-localization-management/requirements.md`
- [x] `specs/3-localization-management/design.md`
- [x] `specs/3-localization-management/tasks.md`

---

## Out-of-scope (deferred follow-ups)

* **Full CLDR `windowsZones.xml` import.** The manager bundles ~30
  of the most common entries. A future revision will replace the
  hard-coded table with a generated artefact pulled from CLDR's
  upstream mapping data.
* **Multi-language preference writes.** The pilot writes a
  single-element `LanguageList` via `Set-WinUserLanguageList`. A
  `set_languages(list[str])` verb is tracked here as a follow-up.
* **Per-entry `InputMethodTips` writes.** The cmdlet supports
  binding specific TIPs to each language in the list; the pilot
  writes only the language tag and lets Windows pick a default
  input method.
* **`Set-WinHomeLocation` (region codes).** Out of scope per the
  cross-OS contract.
* **Locale code-page surface.** Win32 ANSI / OEM code pages
  (e.g. ``Get-WinSystemLocale | Select OEMCodePage``) are not
  exposed by this pilot.
* **`requires_reboot` for `Set-WinUserLanguageList`.** Some
  enterprise deployments surface a "language preference change
  takes effect after sign-out" prompt; the pilot does not try to
  detect this and leaves `requires_reboot=False` for the keymap
  verb.
* **`WindowsTimezoneInfo`.** `precision-time-management` owns
  `dst_active` and `utc_offset_seconds` on Windows. If the
  cross-OS contract collapses the overlap, this manager will grow
  a full `WindowsTimezoneInfo`.
* **`Get-WinDefaultInputMethodOverride` parity.** Reading the
  user's *default* input method override (separate from the
  language list head) is tracked as a follow-up.
