"""
    Windows localization-management feature.

    Implements the cross-OS localization-management contract for Windows
    using PowerShell cmdlets:

        * ``Get-Culture`` / ``Set-Culture`` — current-user culture
          (``HKCU\\Control Panel\\International``).
        * ``Get-WinSystemLocale`` / ``Set-WinSystemLocale`` —
          system-wide locale.  Mutating this requires Administrator
          privileges AND a reboot to take effect.
        * ``Get-WinUserLanguageList`` / ``Set-WinUserLanguageList`` —
          ordered list of user-preferred languages (and their input
          methods); the manager treats the head of this list as the
          "primary keymap" for cross-OS parity.
        * ``Get-TimeZone`` / ``Set-TimeZone`` — system timezone using
          Windows-format IDs (``"Pacific Standard Time"``).
        * ``[System.Globalization.CultureInfo]::GetCultures(...)`` —
          enumeration of every culture .NET knows about.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks on interactive
        input.

    Reads use ``| ConvertTo-Json -Compress -Depth 5`` for a single
    robust serialization that round-trips across PowerShell 5.1 and
    PowerShell 7+.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator` and never imports ``subprocess``.

    Elevation policy:
        Reads (``get_locale``, ``list_supported_locales``,
        ``get_keymap``, ``get_supported_keymaps``, ``get_timezone``,
        ``get_supported_timezones``, every idempotency probe) never
        elevate.  Writes honour ``config.elevated``.

    Per-user vs system-wide locale:
        ``Set-Culture`` mutates the *current user's* locale and does
        NOT require Administrator privileges.  ``Set-WinSystemLocale``
        mutates the *machine-wide* locale and DOES require
        Administrator privileges AND a reboot to take full effect.
        The manager dispatches per-user writes by default; callers
        opt in to system-wide writes via
        ``WindowsLocalizationManagerConfig.apply_system_wide=True``.

    IANA <-> Windows timezone mapping:
        ``Set-TimeZone -Id`` requires Windows-format timezone IDs
        (``"Pacific Standard Time"``), NOT IANA-style strings
        (``"America/Los_Angeles"``).  The cross-OS contract has
        callers passing IANA-style values, so the manager translates
        via a bundled table of the ~30 most common IANA -> Windows
        mappings.  When the IANA value is already a Windows-format
        ID (i.e. it appears as a value in the table or matches the
        identity ``id`` of any entry) the manager dispatches it
        unchanged.  When neither match succeeds the manager returns
        ``FAILED`` with ``message="unknown_timezone_id"``.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------


_DEFAULT_POWERSHELL = "powershell.exe"
_PS_NOPROFILE = "-NoProfile"
_PS_NONINTERACTIVE = "-NonInteractive"
_PS_COMMAND = "-Command"


# Bundled mapping of the ~30 most common IANA timezones to Windows
# timezone IDs.  Sourced from the CLDR
# windowsZones.xml mapping (https://cldr.unicode.org/) and trimmed to
# the entries an ops engineer is most likely to encounter.  Callers
# whose IANA value is not in this table get a synthetic
# ``unknown_timezone_id`` failure.
_IANA_TO_WINDOWS_TZ: dict = {
    "Africa/Cairo":          "Egypt Standard Time",
    "Africa/Johannesburg":   "South Africa Standard Time",
    "Africa/Lagos":          "W. Central Africa Standard Time",
    "Africa/Nairobi":        "E. Africa Standard Time",
    "America/Anchorage":     "Alaskan Standard Time",
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "America/Bogota":        "SA Pacific Standard Time",
    "America/Chicago":       "Central Standard Time",
    "America/Denver":        "Mountain Standard Time",
    "America/Halifax":       "Atlantic Standard Time",
    "America/Los_Angeles":   "Pacific Standard Time",
    "America/Mexico_City":   "Central Standard Time (Mexico)",
    "America/New_York":      "Eastern Standard Time",
    "America/Phoenix":       "US Mountain Standard Time",
    "America/Sao_Paulo":     "E. South America Standard Time",
    "America/Toronto":       "Eastern Standard Time",
    "Asia/Dubai":            "Arabian Standard Time",
    "Asia/Hong_Kong":        "China Standard Time",
    "Asia/Jerusalem":        "Israel Standard Time",
    "Asia/Kolkata":          "India Standard Time",
    "Asia/Seoul":            "Korea Standard Time",
    "Asia/Shanghai":         "China Standard Time",
    "Asia/Singapore":        "Singapore Standard Time",
    "Asia/Tokyo":            "Tokyo Standard Time",
    "Australia/Brisbane":    "E. Australia Standard Time",
    "Australia/Perth":       "W. Australia Standard Time",
    "Australia/Sydney":      "AUS Eastern Standard Time",
    "Europe/Berlin":         "W. Europe Standard Time",
    "Europe/Istanbul":       "Turkey Standard Time",
    "Europe/London":         "GMT Standard Time",
    "Europe/Madrid":         "Romance Standard Time",
    "Europe/Moscow":         "Russian Standard Time",
    "Europe/Paris":          "Romance Standard Time",
    "Europe/Rome":           "W. Europe Standard Time",
    "Europe/Warsaw":         "Central European Standard Time",
    "Pacific/Auckland":      "New Zealand Standard Time",
    "Pacific/Honolulu":      "Hawaiian Standard Time",
    "UTC":                   "UTC",
    "Etc/UTC":               "UTC",
}


WindowsLocalizationActionStatus = ActionStatus


@dataclass
class WindowsLocalizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsLocalizationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell executable
                                      used to run cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-resolved
                                      Windows PowerShell 5.1).  Set to
                                      ``"pwsh.exe"`` to target
                                      PowerShell 7+.
        :param apply_system_wide: When True, ``set_locale`` additionally
                                  dispatches ``Set-WinSystemLocale`` to
                                  mutate the machine-wide locale.  This
                                  requires Administrator privileges AND
                                  a reboot to take full effect; the
                                  result's ``requires_reboot`` field is
                                  set accordingly.  When False (the
                                  default), ``set_locale`` mutates only
                                  the current user's culture via
                                  ``Set-Culture``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    apply_system_wide: bool = False


@dataclass
class WindowsLocalizationActionResult(FeatureActionResult):
    """
        Result of a localization-management write operation.

        :param setting: The localization field targeted: one of
                        ``"locale"``, ``"keymap"``, or ``"timezone"``.
        :param previous_value: The value read prior to the mutation,
                               when the manager was able to probe it.
                               ``None`` when the probe failed.
        :param requires_reboot: True when the underlying mutation
                                requires a reboot to take effect.
                                Set when ``set_locale`` is dispatched
                                under
                                ``apply_system_wide=True`` and the
                                ``Set-WinSystemLocale`` step succeeded
                                (or was the dry-run command).
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes (e.g.
                        ``"unknown_timezone_id"``).
    """
    setting: Optional[str] = None
    previous_value: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class WindowsLocaleInfo:
    """
        Snapshot of the Windows locale state.

        :param language: The active culture name (e.g. ``"en-US"``).
                         BCP 47 hyphenated form.
        :param country: Region tag derived from ``language`` when the
                        ``language`` value contains a hyphen-separated
                        region (e.g. ``"US"`` for ``"en-US"``).
                        ``None`` when not derivable.
        :param encoding: Always ``None`` on Windows — Win32 culture
                         metadata is encoding-agnostic (the CRT exposes
                         a separate code-page surface).
        :param lc_messages: Always ``None`` on Windows — there is no
                            standalone ``LC_MESSAGES`` analogue.
        :param lc_time: Always ``None``.
        :param lc_numeric: Always ``None``.
        :param system_default: ``Get-WinSystemLocale`` ``Name`` value
                               (the machine-wide locale).  Empty
                               string when the read failed.
        :param raw: Mapping carrying the verbatim parsed payloads
                    keyed on ``"culture"`` and ``"system_locale"``.
    """
    language: str
    country: Optional[str] = None
    encoding: Optional[str] = None
    lc_messages: Optional[str] = None
    lc_time: Optional[str] = None
    lc_numeric: Optional[str] = None
    system_default: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class WindowsKeymapInfo:
    """
        Snapshot of the Windows user-language list.

        :param name: The head language tag (e.g. ``"en-US"``).  Empty
                     string when the list is empty.
        :param layout: Always equal to ``name`` on Windows — the
                       cmdlet does not separate the two.
        :param variant: Always ``None`` on Windows.
        :param model: Always ``None`` on Windows.
        :param options: Always ``None`` on Windows.
        :param input_methods: Ordered list of Tablet Input Panel input
                              methods bound to ``name`` (the
                              ``InputMethodTips`` array on the head
                              entry).  Empty when the list is empty.
        :param language_list: Ordered list of every language tag in the
                              user-language list.  Useful when callers
                              want to introspect the full preference
                              order rather than just the head.
    """
    name: str
    layout: Optional[str] = None
    variant: Optional[str] = None
    model: Optional[str] = None
    options: Optional[str] = None
    input_methods: list = field(default_factory=list)
    language_list: list = field(default_factory=list)


class WindowsLocalizationQueryError(Exception):
    """
        Raised when a localization read fails for a reason other than a
        plainly empty payload.
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used when there
                            is no failed argv to report (e.g. JSON parse
                            errors).
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Localization query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single quotes.

        :param value: The raw text.

        :returns: The escaped text suitable for embedding between
                  single quotes in a PowerShell scriptblock.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _normalize_to_list(*, parsed):
    """
        Normalise a ``ConvertTo-Json`` payload into a list.

        ``ConvertTo-Json`` emits a bare object instead of a single-
        element array when the source pipeline produced exactly one
        item.  This helper folds the bare-object shape into a single-
        element list so downstream parsers always see an iterable.

        :param parsed: Output of :func:`json.loads`.

        :returns: A list.  ``None`` and other non-list / non-dict
                  types collapse to an empty list.
    """
    if isinstance(parsed, list):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, dict):
        rtnval = [parsed]
        return rtnval
    rtnval = []
    return rtnval


def _parse_json_object(*, text: str) -> dict:
    """
        Parse a single ``ConvertTo-Json`` object body.

        :param text: Raw stdout.

        :returns: The parsed dict, or an empty dict on failure.
    """
    rtnval: dict = {}
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    if isinstance(parsed, dict):
        rtnval = parsed
        return rtnval
    if isinstance(parsed, list) and len(parsed) > 0:
        first_entry = parsed[0]
        if isinstance(first_entry, dict):
            rtnval = first_entry
            return rtnval
    return rtnval


def _parse_culture_payload(*, text: str) -> str:
    """
        Parse the JSON body emitted by ``Get-Culture | ConvertTo-Json``.

        ``Get-Culture`` exposes ``Name`` (e.g. ``"en-US"``) and a number
        of secondary fields.  We only need ``Name``.

        :param text: Raw stdout.

        :returns: The trimmed culture name, or the empty string on
                  parse failure.
    """
    obj = _parse_json_object(text=text)
    name_raw = obj.get("Name")
    if name_raw is None:
        rtnval = ""
        return rtnval
    rtnval = str(name_raw).strip()
    return rtnval


def _parse_system_locale_payload(*, text: str) -> str:
    """
        Parse the JSON body emitted by
        ``Get-WinSystemLocale | ConvertTo-Json``.

        :param text: Raw stdout.

        :returns: The trimmed locale ``Name`` (e.g. ``"en-US"``), or
                  the empty string on parse failure.
    """
    obj = _parse_json_object(text=text)
    name_raw = obj.get("Name")
    if name_raw is None:
        rtnval = ""
        return rtnval
    rtnval = str(name_raw).strip()
    return rtnval


def _parse_timezone_payload(*, text: str) -> dict:
    """
        Parse the JSON body emitted by
        ``Get-TimeZone | Select Id, DisplayName | ConvertTo-Json``.

        :param text: Raw stdout.

        :returns: A dict with ``"id"`` and ``"display_name"`` keys.
                  Both default to the empty string on parse failure.
    """
    obj = _parse_json_object(text=text)
    rtnval = {
        "id": str(obj.get("Id", "") or "").strip(),
        "display_name": str(obj.get("DisplayName", "") or "").strip(),
    }
    return rtnval


def _parse_user_language_list(*, text: str) -> list:
    """
        Parse ``Get-WinUserLanguageList | ConvertTo-Json``.

        Returns a list of ``{"language_tag": str,
        "input_method_tips": list[str]}`` dicts.

        :param text: Raw stdout.

        :returns: One entry per language in the user list.  An empty
                  list on parse failure.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue
        tag_raw = record.get("LanguageTag")
        if tag_raw is None:
            continue
        tag = str(tag_raw).strip()
        if len(tag) == 0:
            continue

        tips_raw = record.get("InputMethodTips")
        tips: list = []
        if isinstance(tips_raw, list):
            for tip in tips_raw:
                if tip is None:
                    continue
                tip_text = str(tip).strip()
                if len(tip_text) > 0:
                    tips.append(tip_text)

        entry = {
            "language_tag": tag,
            "input_method_tips": tips,
        }
        rtnval.append(entry)
    return rtnval


def _parse_culture_list(*, text: str) -> list:
    """
        Parse the JSON body emitted by
        ``[CultureInfo]::GetCultures(...) | Select Name |
        ConvertTo-Json``.

        :param text: Raw stdout.

        :returns: A list of culture ``Name`` strings.  Empty on parse
                  failure.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue
        name_raw = record.get("Name")
        if name_raw is None:
            continue
        name = str(name_raw).strip()
        if len(name) == 0:
            continue
        rtnval.append(name)
    return rtnval


def _parse_timezone_list(*, text: str) -> list:
    """
        Parse the JSON body emitted by
        ``Get-TimeZone -ListAvailable | Select Id | ConvertTo-Json``.

        :param text: Raw stdout.

        :returns: A list of timezone ``Id`` strings.  Empty on parse
                  failure.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval

    records = _normalize_to_list(parsed=parsed)
    for record in records:
        if not isinstance(record, dict):
            continue
        id_raw = record.get("Id")
        if id_raw is None:
            continue
        id_text = str(id_raw).strip()
        if len(id_text) == 0:
            continue
        rtnval.append(id_text)
    return rtnval


def _derive_country_from_culture(*, language: str) -> Optional[str]:
    """
        Derive the region code from a BCP-47-style culture name.

        Examples::

            "en-US" -> "US"
            "fr-FR" -> "FR"
            "en"    -> None

        :param language: The culture name (BCP 47 hyphenated).

        :returns: The region code, or ``None``.
    """
    if language is None:
        rtnval: Optional[str] = None
        return rtnval
    stripped = language.strip()
    if len(stripped) == 0:
        rtnval = None
        return rtnval
    if "-" not in stripped:
        rtnval = None
        return rtnval
    _, _, country_text = stripped.partition("-")
    if len(country_text) == 0:
        rtnval = None
        return rtnval
    rtnval = country_text
    return rtnval


def _resolve_timezone_id(
    *,
    requested: str,
    available_windows_ids: Optional[list] = None,
) -> Optional[str]:
    """
        Resolve an IANA-or-Windows timezone string to a Windows-format
        timezone ID.

        Resolution order:
            1. Exact key match in ``_IANA_TO_WINDOWS_TZ`` (IANA -> Win).
            2. Exact match against any *value* in
               ``_IANA_TO_WINDOWS_TZ`` (the request was already a
               Windows ID).
            3. Exact match against any entry in
               ``available_windows_ids`` (a live
               ``Get-TimeZone -ListAvailable`` snapshot).
            4. ``None`` — caller surfaces ``unknown_timezone_id``.

        :param requested: The caller-supplied timezone string.
        :param available_windows_ids: Optional live Windows-tz-id list
                                      from a previous
                                      ``get_supported_timezones()``
                                      invocation.

        :returns: A Windows-format timezone ID, or ``None``.
    """
    if requested is None:
        rtnval: Optional[str] = None
        return rtnval
    stripped = requested.strip()
    if len(stripped) == 0:
        rtnval = None
        return rtnval

    if stripped in _IANA_TO_WINDOWS_TZ:
        rtnval = _IANA_TO_WINDOWS_TZ[stripped]
        return rtnval

    for candidate_windows_id in _IANA_TO_WINDOWS_TZ.values():
        if candidate_windows_id == stripped:
            rtnval = stripped
            return rtnval

    if available_windows_ids is not None:
        for candidate in available_windows_ids:
            if candidate == stripped:
                rtnval = stripped
                return rtnval

    rtnval = None
    return rtnval


# ----------------------------------------------------------------------
# WindowsLocalizationManager
# ----------------------------------------------------------------------


class WindowsLocalizationManager(BaseFeatureManager):
    """
        Windows localization manager.  Wraps PowerShell cmdlets through
        the injected :class:`ICommandRunner`.  Reads never elevate;
        writes elevate when ``config.elevated`` is True.
    """

    result_class = WindowsLocalizationActionResult
    status_class = WindowsLocalizationActionStatus
    feature_name = "localization-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_locale(self) -> WindowsLocaleInfo:
        """
            Read the active culture via ``Get-Culture | ConvertTo-Json
            -Compress -Depth 5`` plus ``Get-WinSystemLocale | ...``.

            :returns: The :class:`WindowsLocaleInfo`.
            :raises WindowsLocalizationQueryError: When ``Get-Culture``
                                                   exits non-zero with
                                                   stderr.
        """
        culture_script = (
            "Get-Culture | ConvertTo-Json -Compress -Depth 5"
        )
        culture_command = self._build_powershell_argv(script=culture_script)
        culture_run = self._runner.run(list(culture_command), elevated=False)
        if culture_run.exit_code != 0:
            stderr_text = culture_run.stderr or ""
            if len(stderr_text.strip()) > 0:
                raise WindowsLocalizationQueryError(
                    command=culture_command,
                    exit_code=culture_run.exit_code,
                    stderr=stderr_text,
                )

        system_script = (
            "Get-WinSystemLocale | ConvertTo-Json -Compress -Depth 5"
        )
        system_command = self._build_powershell_argv(script=system_script)
        system_run = self._runner.run(list(system_command), elevated=False)
        if system_run.exit_code != 0:
            system_locale_text = ""
        else:
            system_locale_text = system_run.stdout

        culture_name = _parse_culture_payload(text=culture_run.stdout)
        system_name = _parse_system_locale_payload(text=system_locale_text)
        country = _derive_country_from_culture(language=culture_name)

        info = WindowsLocaleInfo(
            language=culture_name,
            country=country,
            encoding=None,
            lc_messages=None,
            lc_time=None,
            lc_numeric=None,
            system_default=system_name,
            raw={
                "culture": culture_name,
                "system_locale": system_name,
            },
        )
        return info

    def list_supported_locales(self) -> list:
        """
            Enumerate every culture .NET knows about via
            ``[CultureInfo]::GetCultures(AllCultures) | Select Name``.

            :returns: A list of culture names.
            :raises WindowsLocalizationQueryError: When the cmdlet
                                                   exits non-zero.
        """
        script = (
            "[System.Globalization.CultureInfo]::"
            "GetCultures([System.Globalization.CultureTypes]::AllCultures) "
            "| Select-Object Name "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_culture_list(text=run_result.stdout)
        return rtnval

    def get_keymap(self) -> WindowsKeymapInfo:
        """
            Read the user's preferred-language list via
            ``Get-WinUserLanguageList | ConvertTo-Json``.

            :returns: The :class:`WindowsKeymapInfo`.
            :raises WindowsLocalizationQueryError: When the cmdlet
                                                   exits non-zero with
                                                   stderr.
        """
        script = (
            "Get-WinUserLanguageList "
            "| Select-Object LanguageTag, InputMethodTips "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if len(stderr_text.strip()) > 0:
                raise WindowsLocalizationQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=stderr_text,
                )

        entries = _parse_user_language_list(text=run_result.stdout)
        if len(entries) > 0:
            head = entries[0]
            head_tag = head["language_tag"]
            head_tips = list(head["input_method_tips"])
        else:
            head_tag = ""
            head_tips = []
        language_list = [entry["language_tag"] for entry in entries]

        info = WindowsKeymapInfo(
            name=head_tag,
            layout=head_tag if len(head_tag) > 0 else None,
            variant=None,
            model=None,
            options=None,
            input_methods=head_tips,
            language_list=language_list,
        )
        return info

    def get_supported_keymaps(self) -> list:
        """
            Enumerate input methods the user can pick from via
            ``Get-WinUserLanguageList`` (best-effort).

            Windows does not expose a flat keymap list comparable to
            Linux's ``localectl list-keymaps``; the manager surfaces
            the union of every language tag and every Tablet Input
            Panel input-method TIP currently configured for the user.

            :returns: An ordered list of language tags followed by the
                      union of their input-method TIPs.
        """
        info = self.get_keymap()
        seen = set()
        rtnval: list = []
        for tag in info.language_list:
            if tag in seen:
                continue
            seen.add(tag)
            rtnval.append(tag)
        for tip in info.input_methods:
            if tip in seen:
                continue
            seen.add(tip)
            rtnval.append(tip)
        return rtnval

    def get_timezone(self) -> str:
        """
            Read the active system timezone via ``Get-TimeZone | Select
            Id, DisplayName | ConvertTo-Json``.

            :returns: The Windows-format timezone ID (e.g.
                      ``"Pacific Standard Time"``).  The empty string
                      on parse failure.
            :raises WindowsLocalizationQueryError: When the cmdlet
                                                   exits non-zero.
        """
        script = (
            "Get-TimeZone | Select-Object Id, DisplayName "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        parsed = _parse_timezone_payload(text=run_result.stdout)
        rtnval = parsed["id"]
        return rtnval

    def get_supported_timezones(self) -> list:
        """
            Enumerate available Windows-format timezone IDs via
            ``Get-TimeZone -ListAvailable | Select Id``.

            :returns: A list of Windows timezone IDs.
            :raises WindowsLocalizationQueryError: When the cmdlet
                                                   exits non-zero.
        """
        script = (
            "Get-TimeZone -ListAvailable | Select-Object Id "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_timezone_list(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_locale(
        self,
        locale: str,
    ) -> WindowsLocalizationActionResult:
        """
            Set the user's culture via ``Set-Culture -CultureInfo
            '<locale>'``.

            When the manager's
            ``WindowsLocalizationManagerConfig.apply_system_wide`` is
            True, after the per-user write succeeds the manager
            additionally dispatches ``Set-WinSystemLocale -SystemLocale
            '<locale>'``.  ``Set-WinSystemLocale`` requires
            Administrator privileges and a reboot to take full effect;
            the result reports ``requires_reboot=True``.

            Idempotent: when the parsed user culture already matches
            the requested value (and, when ``apply_system_wide`` is
            True, the parsed system locale also matches), returns
            ``SKIPPED``.

            :param locale: The desired BCP-47 culture name (e.g.
                           ``"en-US"``).

            :returns: The :class:`WindowsLocalizationActionResult`.
        """
        cfg = self._typed_config()
        escaped_locale = _ps_escape_single_quoted(value=locale)
        culture_script = (
            "Set-Culture -CultureInfo '{}'"
        ).format(escaped_locale)
        culture_command = self._build_powershell_argv(script=culture_script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsLocalizationActionStatus.DRY_RUN,
                setting="locale",
                detail=locale,
                command=culture_command,
                requires_reboot=cfg.apply_system_wide,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_locale()
            previous_value = current.language
            user_matches = current.language == locale
            system_matches = current.system_default == locale
            if cfg.apply_system_wide:
                already_matches = user_matches and system_matches
            else:
                already_matches = user_matches
            if already_matches:
                skipped = self._build_result(
                    status=WindowsLocalizationActionStatus.SKIPPED,
                    setting="locale",
                    detail=locale,
                    previous_value=previous_value,
                    command=culture_command,
                    message="user culture already matches",
                )
                return skipped
        except WindowsLocalizationQueryError:
            previous_value = None

        primary_result = self._dispatch_localization_write(
            command=culture_command,
            setting="locale",
            requested=locale,
            previous_value=previous_value,
            requires_reboot=False,
        )
        if primary_result.status != WindowsLocalizationActionStatus.SUCCESS:
            return primary_result

        if not cfg.apply_system_wide:
            return primary_result

        system_script = (
            "Set-WinSystemLocale -SystemLocale '{}'"
        ).format(escaped_locale)
        system_command = self._build_powershell_argv(script=system_script)
        system_result = self._dispatch_localization_write(
            command=system_command,
            setting="locale",
            requested=locale,
            previous_value=previous_value,
            requires_reboot=True,
        )
        return system_result

    def set_keymap(self, keymap: str) -> WindowsLocalizationActionResult:
        """
            Set the user's preferred-language list via
            ``Set-WinUserLanguageList -LanguageList '<lang>' -Force``.

            On Windows the "keymap" is the head of the user-language
            list (the cmdlet binds language to its preferred input
            method).  This pilot writes a single-element list; multi-
            language preferences are tracked as a follow-up.

            Idempotent: when the parsed head language tag equals the
            requested value, returns ``SKIPPED``.

            :param keymap: The desired BCP-47 language tag (e.g.
                           ``"en-US"``).

            :returns: The :class:`WindowsLocalizationActionResult`.
        """
        escaped = _ps_escape_single_quoted(value=keymap)
        script = (
            "Set-WinUserLanguageList -LanguageList '{}' -Force"
        ).format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsLocalizationActionStatus.DRY_RUN,
                setting="keymap",
                detail=keymap,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_keymap()
            previous_value = current.name
            if current.name == keymap:
                skipped = self._build_result(
                    status=WindowsLocalizationActionStatus.SKIPPED,
                    setting="keymap",
                    detail=keymap,
                    previous_value=previous_value,
                    command=command,
                    message="head language already matches",
                )
                return skipped
        except WindowsLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="keymap",
            requested=keymap,
            previous_value=previous_value,
            requires_reboot=False,
        )
        return rtnval

    def set_timezone(self, timezone: str) -> WindowsLocalizationActionResult:
        """
            Set the system timezone via ``Set-TimeZone -Id '<id>'``.

            Accepts EITHER an IANA-style timezone (``"America/Los_Angeles"``)
            OR a Windows-format timezone ID (``"Pacific Standard Time"``).
            Resolution happens via the bundled ``_IANA_TO_WINDOWS_TZ``
            table; values that are already Windows IDs (i.e. appear as
            a value in the table) pass through unchanged.  When the
            value cannot be resolved THE manager returns ``FAILED``
            with ``message="unknown_timezone_id"``.

            Idempotent: when the parsed Windows timezone ID matches
            the resolved Windows ID, returns ``SKIPPED``.  Comparison
            is case-sensitive on Windows IDs, matching the cmdlet's
            own behaviour.

            :param timezone: The desired timezone (IANA-form or
                             Windows-form).

            :returns: The :class:`WindowsLocalizationActionResult`.
        """
        windows_id = _resolve_timezone_id(requested=timezone)
        if windows_id is None:
            failed = self._build_result(
                status=WindowsLocalizationActionStatus.FAILED,
                setting="timezone",
                detail=timezone,
                command=[],
                message="unknown_timezone_id",
                stderr=(
                    "no IANA->Windows mapping for {!r}"
                ).format(timezone),
            )
            return failed

        escaped = _ps_escape_single_quoted(value=windows_id)
        script = "Set-TimeZone -Id '{}'".format(escaped)
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsLocalizationActionStatus.DRY_RUN,
                setting="timezone",
                detail=windows_id,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            previous_value = self.get_timezone()
            if previous_value == windows_id:
                skipped = self._build_result(
                    status=WindowsLocalizationActionStatus.SKIPPED,
                    setting="timezone",
                    detail=windows_id,
                    previous_value=previous_value,
                    command=command,
                    message="timezone already matches",
                )
                return skipped
        except WindowsLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="timezone",
            requested=windows_id,
            previous_value=previous_value,
            requires_reboot=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsLocalizationManagerConfig:
        """
            Return the manager's config typed.

            :returns: The typed configuration.
        """
        if isinstance(self._config, WindowsLocalizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsLocalizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical PowerShell argv for a scriptblock.

            :param script: PowerShell scriptblock text.

            :returns: The full argv list.
        """
        rtnval = [
            self._typed_config().powershell_executable,
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _dispatch_localization_write(
        self,
        *,
        command: list,
        setting: str,
        requested: str,
        previous_value: Optional[str],
        requires_reboot: bool,
    ) -> WindowsLocalizationActionResult:
        """
            Dispatch a mutating localization command.

            :param command: The argv list.
            :param setting: One of ``"locale"``, ``"keymap"``,
                            ``"timezone"``.
            :param requested: The requested value.
            :param previous_value: The previously-read value, if any.
            :param requires_reboot: True when the underlying mutation
                                    requires a reboot to take effect.

            :returns: The :class:`WindowsLocalizationActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsLocalizationActionStatus.SUCCESS
            mapped_reboot = requires_reboot
        else:
            mapped_status = WindowsLocalizationActionStatus.FAILED
            mapped_reboot = False

        rtnval = WindowsLocalizationActionResult(
            status=mapped_status,
            detail=requested,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=mapped_reboot,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsLocalizationActionStatus,
        setting: Optional[str],
        detail: Optional[str],
        command: list,
        previous_value: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
    ) -> WindowsLocalizationActionResult:
        """
            Construct a :class:`WindowsLocalizationActionResult`
            without invoking the runner.

            :returns: The :class:`WindowsLocalizationActionResult`.
        """
        rtnval = WindowsLocalizationActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=requires_reboot,
            message=message,
        )
        return rtnval
