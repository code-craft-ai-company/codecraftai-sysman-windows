"""
    Windows security-compliance feature -- pilot skeleton.

    Windows compliance is markedly different from OpenSCAP / mSCP.
    This implementation provides a primitive-aggregating skeleton that
    composes a :class:`WindowsComplianceReport` from a small set of
    well-defined Windows primitives:

        * ``secedit /export /cfg <path>`` -- exports the local
          security policy as an INF file.  This pilot reads the
          exported file back via ``Get-Content`` and surfaces a
          handful of well-known section/key combinations as
          findings.
        * ``Get-MpComputerStatus | ConvertTo-Json`` -- Windows
          Defender real-time protection state.
        * ``Get-NetFirewallProfile | ConvertTo-Json`` -- Windows
          Firewall enabled state per profile.
        * ``(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\
          CurrentVersion\\Policies\\System' -Name 'EnableLUA').EnableLUA``
          -- UAC enabled state.

    Full compliance-as-code on Windows requires the Microsoft Security
    Compliance Toolkit (LGPO + baseline GPOs, plus the registry.pol
    binary parser).  That work is OUT OF SCOPE for this pilot;
    :meth:`WindowsSecurityComplianceManager.remediate` returns
    ``FAILED`` with an explanatory message when called against any
    finding ID.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner`.  Baseline persistence is read and
        written via PowerShell (``Get-Content`` / ``Set-Content``).
        This module does NOT consume an :class:`IFileOperator`.

    Elevation policy:
        Reads never elevate.  ``run_compliance_profile`` may need
        elevation to read certain registry keys on real hosts but the
        manager forwards ``elevated=False`` so the runner is
        responsible for surfacing a typed FAILED result when reads
        require an Administrator token (mirroring the policy in
        ``servicemanagement.py``).  ``set_baseline`` honours
        ``config.elevated`` for the persistence write.

    Bundled profiles:
        Two pilot profiles are surfaced:

            * ``cis_windows_baseline`` -- CIS Benchmark Windows 10/11
              (Level 1) skeleton.
            * ``microsoft_security_baseline_w11`` -- Microsoft
              Security Baseline Windows 11 skeleton.

        Both profiles drive the same primitive-aggregating
        :func:`_build_findings` -- the differentiation between the
        two is reserved for the eventual Security Compliance
        Toolkit integration.
"""

import datetime
import json
import os

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
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

_DEFAULT_OUTPUT_DIR = r"%ProgramData%\CodeCraftAI\compliance"
_DEFAULT_BASELINE_FILE = "current_baseline.json"

_PROFILE_CIS = "cis_windows_baseline"
_PROFILE_MSFT_W11 = "microsoft_security_baseline_w11"

_BUNDLED_PROFILES_RAW: tuple = (
    {
        "id": _PROFILE_CIS,
        "title": "CIS Windows 10/11 Benchmark (Level 1) -- pilot",
        "description": (
            "Primitive-aggregating skeleton: surfaces UAC, Windows "
            "Defender real-time protection, firewall profile state, "
            "and a curated subset of secedit policy keys."
        ),
    },
    {
        "id": _PROFILE_MSFT_W11,
        "title": "Microsoft Security Baseline Windows 11 -- pilot",
        "description": (
            "Identical primitive surface to cis_windows_baseline; "
            "differentiation reserved for the eventual Security "
            "Compliance Toolkit integration."
        ),
    },
)

_VALID_PROFILE_IDS = (_PROFILE_CIS, _PROFILE_MSFT_W11)


WindowsSecurityComplianceActionStatus = ActionStatus


@dataclass
class WindowsSecurityComplianceManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`WindowsSecurityComplianceManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable.  Defaults to
                                      ``"powershell.exe"``.
        :param output_dir: Base directory under which the baseline
                           JSON and the temporary secedit-export INF
                           file are written.  Defaults to
                           ``%ProgramData%\\CodeCraftAI\\compliance``.
        :param baseline_file: Filename for the persisted baseline
                              JSON.  Defaults to
                              ``current_baseline.json``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    output_dir: str = _DEFAULT_OUTPUT_DIR
    baseline_file: str = _DEFAULT_BASELINE_FILE


@dataclass
class WindowsSecurityComplianceActionResult(FeatureActionResult):
    """
        Result of a security-compliance write operation.

        :param profile: The compliance profile this action targeted,
                        when applicable.
        :param finding_id: The finding ID, when the action targeted a
                           specific finding.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    profile: Optional[str] = None
    finding_id: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsComplianceProfile:
    """
        Compliance profile descriptor surfaced by
        :meth:`WindowsSecurityComplianceManager.list_profiles`.

        :param id: Profile identifier (e.g.
                   ``"cis_windows_baseline"``).
        :param title: Human-readable title.
        :param description: Longer description.
    """
    id: str
    title: str
    description: str = ""


@dataclass
class WindowsComplianceFinding:
    """
        Single compliance finding inside a
        :class:`WindowsComplianceReport`.

        :param rule_id: Stable identifier (e.g.
                        ``"uac.enable_lua"``).
        :param severity: ``"low"``, ``"medium"``, ``"high"``, or
                         ``"critical"``.
        :param title: One-line description of the rule.
        :param description: Optional longer rationale.
        :param status: ``"pass"``, ``"fail"``, ``"warning"``, or
                       ``"unknown"``.
        :param remediation_available: True when an automated
                                      remediation exists for this
                                      rule.  Always False in the
                                      pilot.
        :param remediation_automated: True when the manager will
                                      apply remediation without
                                      operator intervention.  Always
                                      False in the pilot.
    """
    rule_id: str
    severity: str
    title: str
    description: str = ""
    status: str = "unknown"
    remediation_available: bool = False
    remediation_automated: bool = False


@dataclass
class WindowsComplianceReport:
    """
        Composite compliance report returned from
        :meth:`WindowsSecurityComplianceManager.run_compliance_profile`.

        :param profile: The profile ID this report was generated for.
        :param generated_at: ISO-8601 UTC timestamp.
        :param findings: One :class:`WindowsComplianceFinding` per
                         rule.
        :param counts: Map of status -> count (``"pass"``, ``"fail"``,
                       ``"warning"``, ``"unknown"``).
    """
    profile: str
    generated_at: str
    findings: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)


@dataclass
class WindowsBaselineInfo:
    """
        Persisted baseline descriptor.

        :param profile: Profile ID applied as the baseline.
        :param applied_at: ISO-8601 UTC timestamp recorded when
                           :meth:`WindowsSecurityComplianceManager.set_baseline`
                           was invoked.
    """
    profile: str
    applied_at: str


class WindowsSecurityComplianceQueryError(Exception):
    """
        Raised when a compliance read fails for an unrecoverable
        reason (non-zero exit on a step that has no graceful fallback,
        or a JSON payload that fails to parse).
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
            :param stderr: The runner's captured stderr (when
                           relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report.
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
            err_msg_lines = ["Security-compliance query command failed."]
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

        :param value: Raw text.

        :returns: The escaped text.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _utc_now_iso() -> str:
    """
        Return the current UTC time as an ISO-8601 string.

        :returns: The timestamp string.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    rtnval = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return rtnval


def _parse_secedit_inf(*, text: str) -> dict:
    """
        Parse the INI-style INF file produced by ``secedit /export``.

        ``secedit`` emits a UTF-16-LE BOM-prefixed INI file with
        sections such as ``[System Access]`` and
        ``[Registry Values]``.  This helper accepts the already-decoded
        text payload (the manager handles UTF-16 decoding via
        ``Get-Content -Encoding Unicode``).  The parser is lenient:
        unrecognised lines are ignored, and quoted values are
        unwrapped.

        :param text: The decoded INF text.

        :returns: A nested dict of ``{section: {key: value}}``.
    """
    rtnval: dict = {}
    if text is None or len(text) == 0:
        return rtnval

    current_section: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if line.startswith(";") or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            if len(section_name) > 0:
                current_section = section_name
                if current_section not in rtnval:
                    rtnval[current_section] = {}
            continue

        if current_section is None:
            continue

        if "=" not in line:
            continue

        key_part, _, value_part = line.partition("=")
        key = key_part.strip()
        value = value_part.strip()

        if len(value) >= 2:
            if value[0] == '"' and value[-1] == '"':
                value = value[1:-1]

        rtnval[current_section][key] = value

    return rtnval


def _severity_from_rule_id(*, rule_id: str) -> str:
    """
        Map a rule ID to a severity token.  This is a deliberately
        small classification used by the pilot to keep the report
        shape stable.

        :param rule_id: The finding rule ID.

        :returns: ``"low"``, ``"medium"``, ``"high"``, or
                  ``"critical"``.
    """
    if rule_id is None:
        rtnval = "medium"
        return rtnval

    if rule_id.startswith("uac."):
        rtnval = "high"
        return rtnval
    if rule_id.startswith("defender."):
        rtnval = "high"
        return rtnval
    if rule_id.startswith("firewall."):
        rtnval = "high"
        return rtnval
    if rule_id.startswith("password."):
        rtnval = "medium"
        return rtnval
    if rule_id.startswith("audit."):
        rtnval = "medium"
        return rtnval
    rtnval = "medium"
    return rtnval


def _evaluate_uac(*, lua_value: Optional[str]) -> WindowsComplianceFinding:
    """
        Evaluate the UAC EnableLUA value into a
        :class:`WindowsComplianceFinding`.

        :param lua_value: The raw ``EnableLUA`` registry value, or
                          ``None`` when the read failed.

        :returns: The :class:`WindowsComplianceFinding`.
    """
    rule_id = "uac.enable_lua"
    title = "User Account Control (EnableLUA) is enabled."
    description = (
        "EnableLUA must be set to 1 so the Windows logon "
        "workflow uses the protected admin token."
    )
    severity = _severity_from_rule_id(rule_id=rule_id)

    if lua_value is None:
        rtnval = WindowsComplianceFinding(
            rule_id=rule_id,
            severity=severity,
            title=title,
            description=description,
            status="unknown",
        )
        return rtnval

    if str(lua_value).strip() == "1":
        status = "pass"
    else:
        status = "fail"

    rtnval = WindowsComplianceFinding(
        rule_id=rule_id,
        severity=severity,
        title=title,
        description=description,
        status=status,
    )
    return rtnval


def _evaluate_defender(
    *,
    defender_status: Optional[dict],
) -> WindowsComplianceFinding:
    """
        Evaluate Windows Defender ``Get-MpComputerStatus`` JSON into a
        :class:`WindowsComplianceFinding`.

        :param defender_status: Parsed JSON dict, or ``None`` when the
                                read failed.

        :returns: The :class:`WindowsComplianceFinding`.
    """
    rule_id = "defender.realtime_protection_enabled"
    title = "Windows Defender real-time protection is enabled."
    description = (
        "Get-MpComputerStatus.RealTimeProtectionEnabled must be "
        "True for Windows Defender to actively scan for threats."
    )
    severity = _severity_from_rule_id(rule_id=rule_id)

    if defender_status is None:
        rtnval = WindowsComplianceFinding(
            rule_id=rule_id,
            severity=severity,
            title=title,
            description=description,
            status="unknown",
        )
        return rtnval

    raw_value = defender_status.get("RealTimeProtectionEnabled")
    if raw_value is True:
        status = "pass"
    elif raw_value is False:
        status = "fail"
    else:
        status = "unknown"

    rtnval = WindowsComplianceFinding(
        rule_id=rule_id,
        severity=severity,
        title=title,
        description=description,
        status=status,
    )
    return rtnval


def _evaluate_firewall(
    *,
    firewall_profiles: Optional[list],
) -> list:
    """
        Evaluate ``Get-NetFirewallProfile`` JSON into one
        :class:`WindowsComplianceFinding` per profile.

        :param firewall_profiles: Parsed JSON list, or ``None`` when
                                  the read failed.

        :returns: One :class:`WindowsComplianceFinding` per profile.
    """
    rtnval: list = []
    if firewall_profiles is None:
        rule_id = "firewall.all_profiles_enabled"
        rtnval.append(WindowsComplianceFinding(
            rule_id=rule_id,
            severity=_severity_from_rule_id(rule_id=rule_id),
            title="Windows Firewall profiles are enabled.",
            description=(
                "Get-NetFirewallProfile must report Enabled=True "
                "for Domain, Private, and Public profiles."
            ),
            status="unknown",
        ))
        return rtnval

    for profile_record in firewall_profiles:
        if not isinstance(profile_record, dict):
            continue
        profile_name_raw = profile_record.get("Name")
        if profile_name_raw is None:
            continue
        profile_name = str(profile_name_raw)
        rule_id = "firewall.{}.enabled".format(profile_name.lower())
        enabled_raw = profile_record.get("Enabled")
        if enabled_raw is True:
            status = "pass"
        elif enabled_raw is False:
            status = "fail"
        else:
            status = "unknown"

        finding = WindowsComplianceFinding(
            rule_id=rule_id,
            severity=_severity_from_rule_id(rule_id=rule_id),
            title="Windows Firewall {} profile is enabled.".format(
                profile_name,
            ),
            description=(
                "Get-NetFirewallProfile -Name {} must report "
                "Enabled=True."
            ).format(profile_name),
            status=status,
        )
        rtnval.append(finding)

    return rtnval


def _evaluate_secedit(*, inf_dict: dict) -> list:
    """
        Evaluate a small curated subset of secedit policy keys into
        :class:`WindowsComplianceFinding` rows.

        Currently surfaces:

            * ``[System Access].MinimumPasswordLength`` -- pass when
              >= 14.
            * ``[System Access].LockoutBadCount`` -- pass when in
              [1, 10] inclusive.
            * ``[Event Audit].AuditLogonEvents`` -- pass when value
              indicates "Success and Failure" (3).

        :param inf_dict: Parsed secedit INF.

        :returns: Findings list.
    """
    rtnval: list = []

    system_access = inf_dict.get("System Access", {})

    min_pwd_len_raw = system_access.get("MinimumPasswordLength")
    rule_id = "password.minimum_length_ge_14"
    title = "Minimum password length is at least 14 characters."
    description = (
        "[System Access] MinimumPasswordLength must be >= 14."
    )
    if min_pwd_len_raw is None:
        status = "unknown"
    else:
        try:
            min_pwd_len = int(min_pwd_len_raw)
        except (ValueError, TypeError):
            min_pwd_len = -1
        if min_pwd_len < 0:
            status = "unknown"
        elif min_pwd_len >= 14:
            status = "pass"
        else:
            status = "fail"
    rtnval.append(WindowsComplianceFinding(
        rule_id=rule_id,
        severity=_severity_from_rule_id(rule_id=rule_id),
        title=title,
        description=description,
        status=status,
    ))

    lockout_raw = system_access.get("LockoutBadCount")
    rule_id = "password.lockout_threshold_in_range"
    title = "Account lockout threshold is set in the range 1-10."
    description = (
        "[System Access] LockoutBadCount must be > 0 and <= 10."
    )
    if lockout_raw is None:
        status = "unknown"
    else:
        try:
            lockout_value = int(lockout_raw)
        except (ValueError, TypeError):
            lockout_value = -1
        if lockout_value < 0:
            status = "unknown"
        elif lockout_value > 0 and lockout_value <= 10:
            status = "pass"
        else:
            status = "fail"
    rtnval.append(WindowsComplianceFinding(
        rule_id=rule_id,
        severity=_severity_from_rule_id(rule_id=rule_id),
        title=title,
        description=description,
        status=status,
    ))

    event_audit = inf_dict.get("Event Audit", {})
    audit_logon_raw = event_audit.get("AuditLogonEvents")
    rule_id = "audit.logon_events_success_and_failure"
    title = "Audit logon events for success and failure."
    description = (
        "[Event Audit] AuditLogonEvents must be set to 3 (Success "
        "and Failure)."
    )
    if audit_logon_raw is None:
        status = "unknown"
    else:
        try:
            audit_value = int(audit_logon_raw)
        except (ValueError, TypeError):
            audit_value = -1
        if audit_value < 0:
            status = "unknown"
        elif audit_value == 3:
            status = "pass"
        else:
            status = "fail"
    rtnval.append(WindowsComplianceFinding(
        rule_id=rule_id,
        severity=_severity_from_rule_id(rule_id=rule_id),
        title=title,
        description=description,
        status=status,
    ))

    return rtnval


def _build_findings(
    *,
    inf_dict: dict,
    defender_status: Optional[dict],
    firewall_profiles: Optional[list],
    lua_value: Optional[str],
) -> list:
    """
        Build the full findings list from the gathered Windows
        primitives.

        :param inf_dict: Parsed secedit INF.
        :param defender_status: Parsed Defender JSON or None.
        :param firewall_profiles: Parsed firewall JSON list or None.
        :param lua_value: Raw EnableLUA value or None.

        :returns: Findings list.
    """
    rtnval: list = []
    rtnval.append(_evaluate_uac(lua_value=lua_value))
    rtnval.append(_evaluate_defender(defender_status=defender_status))

    firewall_findings = _evaluate_firewall(
        firewall_profiles=firewall_profiles,
    )
    for finding in firewall_findings:
        rtnval.append(finding)

    secedit_findings = _evaluate_secedit(inf_dict=inf_dict)
    for finding in secedit_findings:
        rtnval.append(finding)

    return rtnval


def _count_findings(*, findings: list) -> dict:
    """
        Count findings by status.

        :param findings: List of :class:`WindowsComplianceFinding`.

        :returns: ``{"pass": N, "fail": N, "warning": N, "unknown": N}``.
    """
    counts = {"pass": 0, "fail": 0, "warning": 0, "unknown": 0}
    for finding in findings:
        if finding.status in counts:
            counts[finding.status] = counts[finding.status] + 1
        else:
            counts[finding.status] = 1
    return counts


# ----------------------------------------------------------------------
# WindowsSecurityComplianceManager
# ----------------------------------------------------------------------


class WindowsSecurityComplianceManager(BaseFeatureManager):
    """
        Windows security-compliance manager (pilot skeleton).
    """

    result_class = WindowsSecurityComplianceActionResult
    status_class = WindowsSecurityComplianceActionStatus
    feature_name = "security-compliance"

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_profiles(self) -> list:
        """
            Return the bundled list of compliance profiles.

            :returns: A list of :class:`WindowsComplianceProfile`.
        """
        rtnval: list = []
        for raw in _BUNDLED_PROFILES_RAW:
            profile = WindowsComplianceProfile(
                id=raw["id"],
                title=raw["title"],
                description=raw["description"],
            )
            rtnval.append(profile)
        return rtnval

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run_compliance_profile(
        self,
        profile_name: str,
    ) -> WindowsComplianceReport:
        """
            Run the named compliance profile and return a
            :class:`WindowsComplianceReport`.

            Steps:
                1. Validate ``profile_name`` against the bundled list.
                2. ``secedit /export /cfg <output_dir>\\secpol.inf``
                   then read the file back via ``Get-Content
                   -Encoding Unicode``.
                3. ``Get-MpComputerStatus | ConvertTo-Json -Compress
                   -Depth 5``.
                4. ``Get-NetFirewallProfile | ConvertTo-Json -Compress
                   -Depth 5``.
                5. ``(Get-ItemProperty 'HKLM:\\...\\Policies\\System'
                   -Name 'EnableLUA').EnableLUA``.
                6. Compose the findings list and return the report.

            Reads do not elevate.  Failures of individual primitives
            collapse to ``status="unknown"`` findings; the only hard
            failure is an unknown profile, which raises
            :class:`WindowsSecurityComplianceQueryError`.

            :param profile_name: Profile ID.

            :returns: The :class:`WindowsComplianceReport`.
            :raises WindowsSecurityComplianceQueryError: When
                                                        ``profile_name``
                                                        is not in the
                                                        bundled list.
        """
        if profile_name not in _VALID_PROFILE_IDS:
            err_msg_lines = ["Unknown compliance profile."]
            err_msg_lines.append("    REQUESTED: {!r}".format(profile_name))
            err_msg_lines.append("    KNOWN: {}".format(
                ", ".join(_VALID_PROFILE_IDS),
            ))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsSecurityComplianceQueryError(message=err_msg)

        inf_dict = self._read_secedit_inf()
        defender_status = self._read_defender_status()
        firewall_profiles = self._read_firewall_profiles()
        lua_value = self._read_uac_lua()

        findings = _build_findings(
            inf_dict=inf_dict,
            defender_status=defender_status,
            firewall_profiles=firewall_profiles,
            lua_value=lua_value,
        )
        counts = _count_findings(findings=findings)

        rtnval = WindowsComplianceReport(
            profile=profile_name,
            generated_at=_utc_now_iso(),
            findings=findings,
            counts=counts,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Remediation
    # ------------------------------------------------------------------

    def remediate(
        self,
        finding_id: str,
    ) -> WindowsSecurityComplianceActionResult:
        """
            Apply remediation for a single finding.

            For the pilot, automated remediation is NOT supported on
            Windows because applying baseline-shaped changes
            requires the Microsoft Security Compliance Toolkit
            (LGPO + GPO baselines) which is out of scope.  This
            method always returns ``FAILED`` with
            ``message="remediation_not_supported"`` so cross-OS
            callers receive a typed result rather than a silent
            no-op.  Honours dry-run.

            :param finding_id: The finding ID.

            :returns: The :class:`WindowsSecurityComplianceActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSecurityComplianceActionStatus.DRY_RUN,
                profile=None,
                finding_id=finding_id,
                command=[],
                message="remediation_not_supported",
            )
            return dry

        message = (
            "Windows automated remediation requires the Microsoft "
            "Security Compliance Toolkit (LGPO + GPO baselines), "
            "which is out of scope for this pilot."
        )
        rtnval = self._build_result(
            status=WindowsSecurityComplianceActionStatus.FAILED,
            profile=None,
            finding_id=finding_id,
            command=[],
            message="remediation_not_supported",
            stderr=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Baseline persistence
    # ------------------------------------------------------------------

    def get_baseline(self) -> Optional[WindowsBaselineInfo]:
        """
            Read the persisted baseline JSON.

            Runs ``Get-Content -Path '<output_dir>\\<baseline_file>'``
            via PowerShell.  Returns ``None`` when the file does not
            exist or is empty.

            :returns: The :class:`WindowsBaselineInfo`, or ``None``.
            :raises WindowsSecurityComplianceQueryError: When the
                                                        file exists
                                                        but the JSON
                                                        is corrupt.
        """
        baseline_path = self._baseline_path()
        escaped = _ps_escape_single_quoted(value=baseline_path)
        script = (
            "if (Test-Path -LiteralPath '{path}') "
            "{{ Get-Content -LiteralPath '{path}' -Raw }} "
            "else {{ '' }}"
        ).format(path=escaped)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise WindowsSecurityComplianceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval: Optional[WindowsBaselineInfo] = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError) as parse_error:
            err_msg_lines = ["Failed to parse baseline JSON payload."]
            err_msg_lines.append("    PATH: {}".format(baseline_path))
            err_msg_lines.append("    STDOUT: {}".format(stripped))
            err_msg_lines.append("    PARSE_ERROR: {}".format(parse_error))
            err_msg = os.linesep.join(err_msg_lines)
            raise WindowsSecurityComplianceQueryError(
                message=err_msg,
            ) from None

        if not isinstance(parsed, dict):
            rtnval = None
            return rtnval

        profile_raw = parsed.get("profile")
        applied_raw = parsed.get("applied_at")
        if profile_raw is None or applied_raw is None:
            rtnval = None
            return rtnval

        rtnval = WindowsBaselineInfo(
            profile=str(profile_raw),
            applied_at=str(applied_raw),
        )
        return rtnval

    def set_baseline(
        self,
        spec: WindowsBaselineInfo,
    ) -> WindowsSecurityComplianceActionResult:
        """
            Persist a baseline descriptor by writing the JSON to
            ``<output_dir>\\<baseline_file>``.

            Honours dry-run; honours ``config.elevated`` for the
            persistence write.  Idempotent: when the existing
            baseline already matches ``spec``, returns ``SKIPPED``.

            :param spec: The :class:`WindowsBaselineInfo`.

            :returns: The :class:`WindowsSecurityComplianceActionResult`.
        """
        if spec is None:
            failed = self._build_result(
                status=WindowsSecurityComplianceActionStatus.FAILED,
                profile=None,
                finding_id=None,
                command=[],
                message="spec_required",
            )
            return failed

        if spec.profile not in _VALID_PROFILE_IDS:
            failed = self._build_result(
                status=WindowsSecurityComplianceActionStatus.FAILED,
                profile=spec.profile,
                finding_id=None,
                command=[],
                message="unknown_profile",
            )
            return failed

        baseline_path = self._baseline_path()
        payload = {
            "profile": spec.profile,
            "applied_at": spec.applied_at,
        }
        payload_json = json.dumps(payload)

        escaped_path = _ps_escape_single_quoted(value=baseline_path)
        escaped_payload = _ps_escape_single_quoted(value=payload_json)
        escaped_dir = _ps_escape_single_quoted(
            value=self._typed_config().output_dir,
        )
        script = (
            "if (-not (Test-Path -LiteralPath '{dir}')) "
            "{{ New-Item -ItemType Directory -Path '{dir}' -Force "
            "| Out-Null }}; "
            "Set-Content -LiteralPath '{path}' -Value '{payload}' "
            "-Encoding UTF8"
        ).format(
            dir=escaped_dir,
            path=escaped_path,
            payload=escaped_payload,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsSecurityComplianceActionStatus.DRY_RUN,
                profile=spec.profile,
                finding_id=None,
                command=command,
            )
            return dry

        existing = self._safe_get_baseline()
        if existing is not None:
            if (
                existing.profile == spec.profile
                and existing.applied_at == spec.applied_at
            ):
                skipped = self._build_result(
                    status=WindowsSecurityComplianceActionStatus.SKIPPED,
                    profile=spec.profile,
                    finding_id=None,
                    command=command,
                    message="baseline already in desired state",
                )
                return skipped

        rtnval = self._dispatch(
            command=command,
            detail=spec.profile,
            operation="set_baseline",
            profile=spec.profile,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsSecurityComplianceManagerConfig:
        """
            Return the manager's config typed.

            :returns: The :class:`WindowsSecurityComplianceManagerConfig`.
        """
        if isinstance(self._config, WindowsSecurityComplianceManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsSecurityComplianceManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list[str]:
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

    def _baseline_path(self) -> str:
        """
            Resolve the baseline JSON file path.

            :returns: The full path string.
        """
        cfg = self._typed_config()
        rtnval = "{}\\{}".format(cfg.output_dir, cfg.baseline_file)
        return rtnval

    def _read_secedit_inf(self) -> dict:
        """
            Run ``secedit /export`` and read the resulting INF back.

            Returns an empty dict on any failure (the report's
            ``unknown`` count absorbs the failure).

            :returns: Parsed secedit dict.
        """
        cfg = self._typed_config()
        export_path = "{}\\secpol.inf".format(cfg.output_dir)
        escaped_path = _ps_escape_single_quoted(value=export_path)
        escaped_dir = _ps_escape_single_quoted(value=cfg.output_dir)

        script = (
            "if (-not (Test-Path -LiteralPath '{dir}')) "
            "{{ New-Item -ItemType Directory -Path '{dir}' -Force "
            "| Out-Null }}; "
            "secedit /export /cfg '{path}' | Out-Null; "
            "if (Test-Path -LiteralPath '{path}') "
            "{{ Get-Content -LiteralPath '{path}' -Raw "
            "-Encoding Unicode }} else {{ '' }}"
        ).format(dir=escaped_dir, path=escaped_path)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: dict = {}
            return rtnval

        rtnval = _parse_secedit_inf(text=run_result.stdout or "")
        return rtnval

    def _read_defender_status(self) -> Optional[dict]:
        """
            Run ``Get-MpComputerStatus`` and parse the JSON.

            :returns: The parsed dict, or ``None`` on failure.
        """
        script = (
            "Get-MpComputerStatus "
            "| Select-Object RealTimeProtectionEnabled, "
            "AntivirusEnabled, AMServiceEnabled "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            if len(parsed) == 0:
                rtnval = None
                return rtnval
            first = parsed[0]
            if not isinstance(first, dict):
                rtnval = None
                return rtnval
            rtnval = first
            return rtnval
        if isinstance(parsed, dict):
            rtnval = parsed
            return rtnval

        rtnval = None
        return rtnval

    def _read_firewall_profiles(self) -> Optional[list]:
        """
            Run ``Get-NetFirewallProfile`` and parse the JSON.

            :returns: A list of dicts (one per profile), or ``None``
                      on failure.
        """
        script = (
            "Get-NetFirewallProfile "
            "| Select-Object Name, Enabled, DefaultInboundAction, "
            "DefaultOutboundAction "
            "| ConvertTo-Json -Compress -Depth 5"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[list] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if isinstance(parsed, list):
            rtnval = parsed
            return rtnval
        if isinstance(parsed, dict):
            rtnval = [parsed]
            return rtnval

        rtnval = None
        return rtnval

    def _read_uac_lua(self) -> Optional[str]:
        """
            Read the UAC ``EnableLUA`` registry value.

            :returns: The raw value as a string, or ``None`` on
                      failure.
        """
        script = (
            "(Get-ItemProperty -Path "
            "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"
            "\\Policies\\System' -Name 'EnableLUA').EnableLUA"
        )
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stripped = (run_result.stdout or "").strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        rtnval = stripped
        return rtnval

    def _safe_get_baseline(self) -> Optional[WindowsBaselineInfo]:
        """
            Wrapper around :meth:`get_baseline` that swallows query
            errors so the idempotency guard inside
            :meth:`set_baseline` does not raise on first-run when the
            file genuinely does not exist.

            :returns: The baseline info, or None.
        """
        try:
            rtnval = self.get_baseline()
        except WindowsSecurityComplianceQueryError:
            rtnval = None
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsSecurityComplianceActionStatus,
        profile: Optional[str],
        finding_id: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsSecurityComplianceActionResult:
        """
            Construct a
            :class:`WindowsSecurityComplianceActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param profile: Profile name.
            :param finding_id: Finding ID.
            :param command: Would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr.

            :returns: The
                      :class:`WindowsSecurityComplianceActionResult`.
        """
        rtnval = WindowsSecurityComplianceActionResult(
            status=status,
            detail=profile,
            command=list(command),
            stderr=stderr,
            profile=profile,
            finding_id=finding_id,
            message=message,
        )
        return rtnval
