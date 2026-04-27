# Design

## Overview

`WindowsLocalizationManager` provides idempotent, dry-run-aware
control over a host's locale, keymap, and timezone settings on
Windows. It is a thin facade over the PowerShell cmdlets
`Get-Culture` / `Set-Culture`, `Get-WinSystemLocale` /
`Set-WinSystemLocale`, `Get-WinUserLanguageList` /
`Set-WinUserLanguageList`, `Get-TimeZone` / `Set-TimeZone`, and the
.NET reflective enumerator
`[System.Globalization.CultureInfo]::GetCultures(AllCultures)`.

The module sits in `automojo.sysman.windows.localizationmanagement`
and exports the following OS-prefixed surface:

| Type | Role |
|------|------|
| `WindowsLocalizationManager` | Manager class. |
| `WindowsLocalizationManagerConfig` | Tunable PowerShell path, `apply_system_wide`, plus `dry_run` / `elevated`. |
| `WindowsLocalizationActionStatus` | Alias of the package `ActionStatus`. |
| `WindowsLocalizationActionResult` | Result dataclass with `setting`, `previous_value`, `requires_reboot`, `message`. |
| `WindowsLocaleInfo` | `get_locale` return shape. |
| `WindowsKeymapInfo` | `get_keymap` return shape. |
| `WindowsLocalizationQueryError` | Raised on read-side failures. |

`get_timezone` returns a plain `str` — the Windows-format timezone
ID — per the per-OS verb table.

## Architecture

```
                Maintenance coordinator
                        │
                        │  builds WindowsLocalizationManager(runner, config)
                        ▼
            WindowsLocalizationManager
                        │
                        │  .run(argv, elevated=…)
                        ▼
                ICommandRunner
                        │
              ┌─────────┴────────┐
              ▼                  ▼
       LocalCommandRunner    SSHCommandRunner
              │                  │
              └────────┬─────────┘
                       │  aspects compose here
                       ▼
            (Persistence | Retry | Timeout)
                       │
                       ▼
           powershell.exe / pwsh.exe
```

Every PowerShell scriptblock is dispatched as
`["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
"<script>"]` so that `$PROFILE` cannot mutate cmdlet behaviour and
PowerShell never blocks on interactive input.

`WindowsLocalizationManager` extends `BaseFeatureManager`. The
localization module exposes its own `_dispatch_localization_write`
helper instead of calling `_dispatch` directly so the result
dataclass's per-feature fields (`setting`, `previous_value`,
`requires_reboot`, `message`) are populated explicitly and the
read-before-write probe stays inside the manager.

## Verb-by-verb design

### `get_locale() -> WindowsLocaleInfo`

Argv pair (both `elevated=False`):

* `Get-Culture | ConvertTo-Json -Compress -Depth 5`
* `Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5`

Parser: `_parse_culture_payload` and
`_parse_system_locale_payload` extract the `Name` field from the
parsed JSON object. The country tag is derived via
`_derive_country_from_culture`:

| `Get-Culture.Name` | `country` |
|--------------------|-----------|
| `en-US` | `US` |
| `fr-FR` | `FR` |
| `en` | `None` |

`encoding`, `lc_messages`, `lc_time`, `lc_numeric` are always `None`
on Windows.

Failure surface: a non-zero exit on `Get-Culture` with non-empty
stderr raises `WindowsLocalizationQueryError`. A non-zero exit
without stderr collapses to a parse with empty inputs (returning
`WindowsLocaleInfo(language="")`). A non-zero exit on
`Get-WinSystemLocale` is silent — `system_default` collapses to the
empty string.

### `set_locale(locale) -> WindowsLocalizationActionResult`

Per-user dispatch (always):

```
Set-Culture -CultureInfo '<locale>'
```

System-wide dispatch (when `apply_system_wide=True`, after
`Set-Culture` succeeds):

```
Set-WinSystemLocale -SystemLocale '<locale>'
```

Idempotency: probes via `get_locale()`. When the parsed user
culture matches AND (when `apply_system_wide=True`) the parsed
system locale matches, returns `SKIPPED`.

`dry_run` short-circuits before the read probe. The result's
`requires_reboot` is set when the system-wide dispatch ran (or in
the dry-run command echo when `apply_system_wide=True`).

Failure on `Set-Culture` returns `FAILED` and the system-wide write
is not dispatched.

### `list_supported_locales() -> list[str]`

Argv:

```
[System.Globalization.CultureInfo]::GetCultures([System.Globalization.CultureTypes]::AllCultures)
| Select-Object Name
| ConvertTo-Json -Compress -Depth 5
```

Parser: `_parse_culture_list` returns the list of `Name` strings.

### `get_keymap() -> WindowsKeymapInfo`

Argv:

```
Get-WinUserLanguageList
| Select-Object LanguageTag, InputMethodTips
| ConvertTo-Json -Compress -Depth 5
```

Parser: `_parse_user_language_list` walks the JSON array, normalises
the bare-object shape via `_normalize_to_list`, and returns one
`{"language_tag": str, "input_method_tips": list[str]}` per row.
The head entry's tag is surfaced as `WindowsKeymapInfo.name`; its
input-method TIPs as `input_methods`.

`layout` mirrors `name`. `variant`, `model`, `options` are always
`None`.

`language_list` carries the ordered list of every language tag for
callers that want to introspect the full preference order.

### `set_keymap(keymap) -> WindowsLocalizationActionResult`

Argv:

```
Set-WinUserLanguageList -LanguageList '<keymap>' -Force
```

Idempotency: probes via `get_keymap()`. Skip when the head language
tag equals the requested value.

> **Single-element write.** This pilot writes a single-element
> language list. Multi-language preference writes are tracked as a
> follow-up in `tasks.md`.

### `get_supported_keymaps() -> list[str]`

Wraps `get_keymap()`. Surfaces the deduplicated union of every
parsed language tag and every input-method TIP currently bound to
the head entry. Never raises; failures collapse to an empty list
upstream.

> **Best-effort.** Windows does not expose a flat keymap list
> comparable to Linux's `localectl list-keymaps`. The cross-OS
> contract acknowledges this. Callers requiring richer enumeration
> should consult MSDN's mapping data directly.

### `get_timezone() -> str`

Argv:

```
Get-TimeZone | Select-Object Id, DisplayName
| ConvertTo-Json -Compress -Depth 5
```

Parser: `_parse_timezone_payload` returns a dict with `id` and
`display_name`. `get_timezone` returns the `id` field directly.

### `set_timezone(tz) -> WindowsLocalizationActionResult`

This is the most divergent verb in the cross-OS feature, because
Windows uses Windows-format timezone IDs (`"Pacific Standard Time"`)
while the cross-OS contract has callers passing IANA-style values
(`"America/Los_Angeles"`).

Resolution order (`_resolve_timezone_id`):

1. Exact key match in `_IANA_TO_WINDOWS_TZ`.
2. Exact match against any *value* in `_IANA_TO_WINDOWS_TZ` (the
   request was already a Windows ID).
3. Exact match against any entry in
   `available_windows_ids` (an optional live snapshot from
   `get_supported_timezones()`).
4. `None` — caller surfaces `unknown_timezone_id`.

When the resolution fails:

```python
WindowsLocalizationActionResult(
    status=FAILED,
    setting="timezone",
    detail=timezone,
    command=[],
    message="unknown_timezone_id",
    stderr="no IANA->Windows mapping for ...",
)
```

is returned without dispatching the runner.

When resolution succeeds, argv:

```
Set-TimeZone -Id '<windows-id>'
```

Idempotency: probes via `get_timezone()`. Skip when the parsed
Windows-format ID matches the resolved Windows-format ID.
Comparison is case-sensitive (matching `Set-TimeZone`'s own
behaviour).

### `get_supported_timezones() -> list[str]`

Argv:

```
Get-TimeZone -ListAvailable | Select-Object Id
| ConvertTo-Json -Compress -Depth 5
```

Parser: `_parse_timezone_list` returns the `Id` list.

## IANA <-> Windows timezone mapping

The bundled `_IANA_TO_WINDOWS_TZ` table covers the ~30 most common
timezones. A representative slice:

| IANA | Windows ID |
|------|------------|
| `UTC` | `UTC` |
| `Etc/UTC` | `UTC` |
| `America/Los_Angeles` | `Pacific Standard Time` |
| `America/Denver` | `Mountain Standard Time` |
| `America/Chicago` | `Central Standard Time` |
| `America/New_York` | `Eastern Standard Time` |
| `America/Phoenix` | `US Mountain Standard Time` |
| `America/Toronto` | `Eastern Standard Time` |
| `America/Sao_Paulo` | `E. South America Standard Time` |
| `Europe/London` | `GMT Standard Time` |
| `Europe/Paris` | `Romance Standard Time` |
| `Europe/Berlin` | `W. Europe Standard Time` |
| `Europe/Moscow` | `Russian Standard Time` |
| `Asia/Tokyo` | `Tokyo Standard Time` |
| `Asia/Shanghai` | `China Standard Time` |
| `Asia/Kolkata` | `India Standard Time` |
| `Asia/Dubai` | `Arabian Standard Time` |
| `Asia/Hong_Kong` | `China Standard Time` |
| `Australia/Sydney` | `AUS Eastern Standard Time` |
| `Australia/Brisbane` | `E. Australia Standard Time` |
| `Pacific/Auckland` | `New Zealand Standard Time` |
| `Pacific/Honolulu` | `Hawaiian Standard Time` |
| `Africa/Cairo` | `Egypt Standard Time` |
| `Africa/Johannesburg` | `South Africa Standard Time` |

Sourced from the CLDR `windowsZones.xml` mapping. The table is
intentionally small — a complete CLDR import is tracked as a
follow-up. Callers whose IANA value is not in this table get a
synthetic `unknown_timezone_id` failure with no runner dispatch.

## Cross-OS locale-format mapping

| Concept | Ubuntu | macOS | Windows |
|---------|--------|-------|---------|
| Display form | `en_US.UTF-8` (POSIX) | `en_US` (Apple form) | `en-US` (BCP 47) |
| Tool surface | `localectl set-locale LANG=<…>` | `defaults write .GlobalPreferences AppleLocale '<…>'` | `Set-Culture -CultureInfo '<…>'` |
| Encoding suffix | Required for non-ASCII (`.UTF-8`) | Implicit (UTF-8 system-wide) | N/A (UTF-16 internal) |
| Country code | Underscore-separated | Underscore-separated | Hyphen-separated |
| Validation source | `locale -a` (GNU) | `locale -a` (BSD) | `[CultureInfo]::GetCultures(AllCultures)` |

The Windows manager does NOT auto-convert between forms. Callers
working across OSes are expected to render the per-OS form
explicitly.

## Persistence and elevation policy

| Operation | Persistence scope | Elevation |
|-----------|-------------------|-----------|
| `Get-Culture` (read) | per-user | Never |
| `Get-WinSystemLocale` (read) | system | Never |
| `Get-WinUserLanguageList` (read) | per-user | Never |
| `Get-TimeZone` (read) | system | Never |
| `Get-TimeZone -ListAvailable` (read) | system | Never |
| `[CultureInfo]::GetCultures` (read) | n/a | Never |
| `Set-Culture` (write) | per-user | Honours `config.elevated` (typically NOT needed) |
| `Set-WinSystemLocale` (write) | system | Honours `config.elevated` (REQUIRED for the call to succeed; reboot required for full effect) |
| `Set-WinUserLanguageList` (write) | per-user | Honours `config.elevated` |
| `Set-TimeZone` (write) | system | Honours `config.elevated` (REQUIRED for the call to succeed) |

`Set-Culture` and `Set-WinUserLanguageList` mutate per-user state
and do not require Administrator privileges in normal interactive
sessions. `Set-WinSystemLocale` and `Set-TimeZone` mutate
machine-wide state and DO require Administrator privileges; the
manager honours `config.elevated` so the caller can wire UAC token
elevation into the runner.

## Idempotency table

| Verb | Probe argv | Skip condition |
|------|-----------|---------------|
| `set_locale` | `Get-Culture` (+ `Get-WinSystemLocale`) | parsed culture matches AND (when `apply_system_wide=True`) parsed system locale matches |
| `set_keymap` | `Get-WinUserLanguageList` | parsed head language tag == requested |
| `set_timezone` | `Get-TimeZone` | parsed Windows-format ID == resolved Windows-format ID |

## Result-dataclass contract

`WindowsLocalizationActionResult` extends the package
`FeatureActionResult` with four fields:

| Field | Type | Populated when |
|-------|------|----------------|
| `setting` | `str | None` | Always populated for verbs that target a single field. |
| `previous_value` | `str | None` | Populated when the read probe ran cleanly. |
| `requires_reboot` | `bool` | True when the dispatched mutation was `Set-WinSystemLocale` (or its dry-run echo); false otherwise. |
| `message` | `str | None` | Carries `"unknown_timezone_id"` on the synthetic FAILED path; carries human-readable detail on `SKIPPED` results. |

## Module-level helpers

* `_ps_escape_single_quoted(value)` — escape single quotes for
  PowerShell single-quoted strings.
* `_normalize_to_list(parsed)` — fold the bare-object shape that
  `ConvertTo-Json` emits for single-row pipelines into a one-
  element list.
* `_parse_json_object(text)` — parse a single JSON object body.
* `_parse_culture_payload(text)` — extract the `Name` field from a
  `Get-Culture` payload.
* `_parse_system_locale_payload(text)` — same for
  `Get-WinSystemLocale`.
* `_parse_timezone_payload(text)` — extract `id` and
  `display_name`.
* `_parse_user_language_list(text)` — array-of-records parser for
  `Get-WinUserLanguageList`.
* `_parse_culture_list(text)` — array-of-records parser for
  `[CultureInfo]::GetCultures(...) | Select Name`.
* `_parse_timezone_list(text)` — array-of-records parser for
  `Get-TimeZone -ListAvailable | Select Id`.
* `_derive_country_from_culture(language)` — pure derivation of
  the region tag.
* `_resolve_timezone_id(requested, available_windows_ids=None)` —
  IANA-or-Windows resolution; returns `None` on failure.

Each helper is a pure function so it can be unit-tested in
isolation.

## Failure surface

| Failure | Surface |
|---------|---------|
| `Get-Culture` exits non-zero with stderr | `WindowsLocalizationQueryError` |
| `Get-Culture` exits non-zero with no stderr | Falls through; `WindowsLocaleInfo(language="")` |
| `Get-WinSystemLocale` exits non-zero | Silent; `system_default=""` |
| `Get-WinUserLanguageList` exits non-zero with stderr | `WindowsLocalizationQueryError` |
| `Get-TimeZone` / `Get-TimeZone -ListAvailable` exit non-zero | `WindowsLocalizationQueryError` |
| `[CultureInfo]::GetCultures` exits non-zero | `WindowsLocalizationQueryError` |
| `set_timezone` with unmapped IANA / Windows value | `WindowsLocalizationActionResult(status=FAILED, message="unknown_timezone_id")` (no runner dispatch) |
| `Set-Culture` / `Set-WinSystemLocale` / `Set-WinUserLanguageList` / `Set-TimeZone` exit non-zero | `WindowsLocalizationActionResult(status=FAILED)` |

Reads raise (typed). Writes never raise; failures are surfaced via
the `FAILED` status carrying the captured `exit_code`, `stdout`,
`stderr`, and `command`.

## Aspect-composition guarantees

The manager makes no assumptions about how the runner dispatches a
PowerShell scriptblock. Retry / persistence / timeout aspects
compose transparently:

* Retry can flip a `FAILED` to `SUCCESS` between attempts.
* Persistence aspects MUST NOT mutate the result.
* Timeout aspects raise their own errors, which propagate through
  the runner unchanged.

## Testing strategy

Tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple. No live
PowerShell is invoked. Coverage:

* Read parsers: `Get-Culture` / `Get-WinSystemLocale`,
  `Get-WinUserLanguageList` (multi-row AND single-row bare-object
  shape), `Get-TimeZone`, `Get-TimeZone -ListAvailable`,
  `[CultureInfo]::GetCultures(...)`.
* Write argv shape: `Set-Culture` (with optional `Set-WinSystemLocale`
  follow-up, `requires_reboot=True`), `Set-WinUserLanguageList`,
  `Set-TimeZone` (with both IANA-style and Windows-form inputs).
* IANA -> Windows resolution: dedicated test
  `test_set_timezone_resolves_iana_to_windows_id`.
* Unmapped tz: dedicated test
  `test_set_timezone_unknown_iana_returns_failed` proving zero
  runner calls and `message="unknown_timezone_id"`.
* Idempotency: each write skips when its probe matches.
* Dry-run: each write returns `DRY_RUN` without invoking the runner.
* Elevation: writes elevate when configured; reads never elevate.
* Failure surface: non-zero writes produce `FAILED` with stderr;
  non-zero reads raise `WindowsLocalizationQueryError`.

The minimum 14-test bar is exceeded by the current
`test_localizationmanagement.py` module.

## Out-of-scope notes

* **Full CLDR `windowsZones.xml` import.** The manager bundles ~30
  of the most common entries. A follow-up will collapse the table
  to a generated artefact pulled from CLDR.
* **`Set-WinHomeLocation`.** Region codes are explicitly out of
  scope per the cross-OS contract.
* **Multi-language preference writes.** The pilot writes a
  single-element `LanguageList`; richer preference writes are
  tracked as a follow-up.
* **`requires_reboot` for `Set-Culture`.** Setting the user
  culture takes effect on the next session, not via reboot. The
  result reports `requires_reboot=False`. Only
  `Set-WinSystemLocale` flips the flag.
* **`WindowsTimezoneInfo`.** `precision-time-management` owns
  `dst_active` and `utc_offset_seconds` on Windows, so this pilot
  returns a plain `str` from `get_timezone`.

## Future-extension surface

* A `set_languages(list[str])` verb that writes a multi-element
  `LanguageList`.
* A `get_timezone_info()` verb that returns the full `Get-TimeZone`
  payload (including `BaseUtcOffset`, `SupportsDaylightSavingTime`).
* A pluggable `_IANA_TO_WINDOWS_TZ` table that loads from a
  generated CLDR snapshot at import time.
* A `set_input_methods(list[str])` verb that writes the user
  language list with explicit `InputMethodTips` arrays per entry.
