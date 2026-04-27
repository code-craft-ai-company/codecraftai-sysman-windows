"""
    Windows ACL-management feature.

    Implements the cross-OS ACL-management contract for Windows
    using PowerShell ``Get-Acl`` for reads and ``icacls.exe`` for
    writes — the canonical NTFS DACL toolchain.  Every system command
    is dispatched through the injected :class:`ICommandRunner`; this
    module never imports ``subprocess`` and never touches the
    filesystem directly.

    Verb catalogue:
        * ``get_acl(path)`` -> ``Get-Acl '<path>' | Select-Object
          Owner,Group,Sddl,AccessToString | ConvertTo-Json
          -Compress -Depth 5`` parsed into a :class:`WindowsAclInfo`.
        * ``set_acl(path, acl_spec)`` -> ``icacls '<path>' /reset``
          followed by ``icacls '<path>' /grant[:r] '<principal>:(F)'``
          per ACE.
        * ``add_ace(path, principal, rights)`` -> ``icacls '<path>'
          /grant[:r] '<principal>:(<rights>)'`` (or ``/deny`` for
          deny ACEs).
        * ``remove_ace(path, principal)`` -> ``icacls '<path>'
          /remove '<principal>'``.
        * ``clear_acl(path)`` -> ``icacls '<path>' /reset``.
        * ``copy_acl(src, dst)`` -> a single PowerShell pipeline
          ``Get-Acl '<src>' | Set-Acl '<dst>'``.
        * ``list_aces(path)`` -> ``Get-Acl '<path>' | Select-Object
          -ExpandProperty Access | ConvertTo-Json -Compress -Depth
          5`` parsed into a list of :class:`WindowsAceInfo`.

    Windows ACL model:
        Windows uses an NT DACL model.  ACEs carry an explicit
        ``Allow`` / ``Deny`` access-control type, an enum
        ``[FileSystemRights]`` rights set (``FullControl``,
        ``Modify``, ``ReadAndExecute``, ``Read``, ``Write``,
        ``ListDirectory``, ``ReadData``, ``WriteData``, ``Delete``,
        ``Synchronize``, ``ExecuteFile``, ``ReadAttributes``,
        ``WriteAttributes``, ``ReadPermissions``,
        ``ChangePermissions``, ``TakeOwnership``, ...), and per-ACE
        inheritance flags (``InheritanceFlags`` of ``ContainerInherit``,
        ``ObjectInherit``, plus ``PropagationFlags`` of
        ``InheritOnly``, ``NoPropagateInherit``).  NTFS does not have
        a "default ACL" partition like POSIX; inheritance is per-ACE
        flag-driven.

    Elevation policy:
        Every write verb (``set_acl``, ``add_ace``, ``remove_ace``,
        ``clear_acl``, ``copy_acl``) honours ``config.elevated``.
        Reads (``get_acl``, ``list_aces``, every idempotency probe)
        never elevate.

    Idempotency:
        * ``set_acl`` reads the current ACL and skips when the
          (principal + permission + rights-set + inheritance)
          tuples match.
        * ``add_ace`` skips when an ACE matching that tuple is
          already present.
        * ``remove_ace`` skips when no ACE matches the principal.
        * ``clear_acl`` skips when only inherited ACEs are present
          (`Get-Acl` reports the canonical "reset" state as
          inherited-only).
        * ``copy_acl`` skips when source and destination SDDL
          strings (the canonical NTFS-native serialisation) are
          equal.

    PowerShell argv shape:
        Every PowerShell scriptblock is dispatched as
        ``["powershell.exe", "-NoProfile", "-NonInteractive",
        "-Command", "<script>"]`` so that ``$PROFILE`` cannot mutate
        cmdlet behaviour and PowerShell never blocks waiting for
        interactive input.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
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

_ICACLS = "icacls.exe"

# ``icacls`` flag tokens.  Documented at ``icacls /?``.  ``/reset``
# replaces every explicit ACE on the path with inheritance-only;
# ``/grant:r`` replaces any matching ACE for a principal; ``/deny``
# adds a deny ACE; ``/remove`` deletes every ACE for a principal.
_ICACLS_RESET = "/reset"
_ICACLS_GRANT_REPLACE = "/grant:r"
_ICACLS_DENY = "/deny"
_ICACLS_REMOVE = "/remove"


WindowsAclActionStatus = ActionStatus


class WindowsAcePermission(Enum):
    """
        ACE polarity.  NTFS DACL ACEs carry an explicit allow / deny
        bit (``System.Security.AccessControl.AccessControlType``).
    """
    ALLOW = "Allow"
    DENY = "Deny"


class WindowsAceInheritance(Enum):
    """
        Inheritance flag set rendered on ``icacls /grant`` and
        comparable against ``Get-Acl``'s ``InheritanceFlags`` field.

        NONE -> no inheritance flags (this object only).
        OBJECT_INHERIT -> ``OI`` flag (apply to child files).
        CONTAINER_INHERIT -> ``CI`` flag (apply to child folders).
        BOTH -> both ``OI`` and ``CI`` flags.
    """
    NONE = "None"
    OBJECT_INHERIT = "ObjectInherit"
    CONTAINER_INHERIT = "ContainerInherit"
    BOTH = "Both"


@dataclass
class WindowsAclManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsAclManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param powershell_executable: Path to the PowerShell
                                      executable used to run
                                      cmdlets.  Defaults to
                                      ``"powershell.exe"`` (PATH-
                                      resolved Windows PowerShell
                                      5.1).
        :param icacls_executable: Path / name of the ``icacls.exe``
                                  binary.  Defaults to ``"icacls.exe"``.
    """
    powershell_executable: str = _DEFAULT_POWERSHELL
    icacls_executable: str = _ICACLS


@dataclass
class WindowsAclActionResult(FeatureActionResult):
    """
        Result of an ACL-management write operation.

        :param path: The target path, when applicable.
        :param principal: The principal acted upon, when applicable.
                          ``None`` for verbs whose write target spans
                          many principals.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsAceInfo:
    """
        A single parsed NTFS DACL entry.

        :param principal: ``DOMAIN\\name`` or builtin name (e.g.
                          ``"BUILTIN\\Administrators"``).  Whatever
                          ``IdentityReference.Value`` reported.
        :param permission: ``ALLOW`` or ``DENY``.
        :param rights: List of ``[FileSystemRights]`` token strings
                       (e.g. ``["Modify", "Synchronize"]``).
        :param inheritance: Parsed inheritance flag set.
        :param is_inherited: True when ``IsInherited`` was true on
                             ``Get-Acl`` -- distinguishes the
                             explicit ACEs we can mutate from the
                             inherited ones we cannot.
    """
    principal: str
    permission: WindowsAcePermission
    rights: list = field(default_factory=list)
    inheritance: WindowsAceInheritance = WindowsAceInheritance.NONE
    is_inherited: bool = False


@dataclass
class WindowsAceSpec:
    """
        Caller-supplied ACE shape used by
        :meth:`WindowsAclManager.set_acl` and
        :meth:`WindowsAclManager.add_ace`.

        :param principal: ``DOMAIN\\name`` or builtin name.
        :param permission: ``ALLOW`` or ``DENY``.
        :param rights: List of ``[FileSystemRights]`` token strings.
        :param inheritance: Optional inheritance flag set.
                            Defaults to ``None`` (rendered as
                            ``WindowsAceInheritance.NONE``).
    """
    principal: str
    permission: WindowsAcePermission
    rights: list
    inheritance: Optional[WindowsAceInheritance] = None


@dataclass
class WindowsAclInfo:
    """
        Snapshot of a path's NTFS ACL.

        :param path: The path the ACL was queried for.
        :param owner: Owner principal name (``Get-Acl`` ``Owner``
                      field).
        :param group: ``Group`` field as reported by ``Get-Acl``.
                      NTFS rarely populates this; usually ``None``.
        :param aces: ACEs in source order.
        :param default_aces: Empty on Windows; NTFS has no "default
                             ACL" partition.  Maintained on the
                             dataclass for cross-OS-contract
                             parity.
        :param sddl: The Security Descriptor Definition Language
                     string from ``Get-Acl`` -- the canonical
                     round-trip serialisation used by ``copy_acl``
                     for byte-exact equality comparison.
    """
    path: str
    owner: Optional[str] = None
    group: Optional[str] = None
    aces: list = field(default_factory=list)
    default_aces: list = field(default_factory=list)
    sddl: Optional[str] = None


class WindowsAclQueryError(Exception):
    """
        Raised when an ACL read fails for a reason other than the
        target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["ACL query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsAclManager(BaseFeatureManager):
    """
        Windows ACL manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; the manager never imports
        ``subprocess`` and never touches the filesystem directly.
    """

    result_class = WindowsAclActionResult
    status_class = WindowsAclActionStatus
    feature_name = "acl-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_acl(self, path: str) -> WindowsAclInfo:
        """
            Return a :class:`WindowsAclInfo` snapshot of ``path``'s
            current DACL.

            Runs a PowerShell scriptblock that captures the
            ``Owner``, ``Group``, ``Sddl``, and per-ACE ``Access``
            rows in a single ``ConvertTo-Json`` payload.

            :param path: The absolute path to query.

            :returns: The parsed :class:`WindowsAclInfo`.

            :raises: :class:`WindowsAclQueryError`
        """
        escaped_path = _ps_escape_single_quoted(value=path)
        script = (
            "$acl = Get-Acl -Path '{}'; "
            "$payload = [PSCustomObject]@{{"
            "Owner = $acl.Owner; "
            "Group = $acl.Group; "
            "Sddl = $acl.Sddl; "
            "Access = $acl.Access"
            "}}; "
            "$payload | ConvertTo-Json -Depth 5 -Compress"
        ).format(escaped_path)
        command = self._build_powershell_argv(script=script)

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise WindowsAclQueryError(
                command=list(command),
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_get_acl_payload(
            output=run_result.stdout,
            path=path,
        )
        return rtnval

    def list_aces(self, path: str) -> list:
        """
            Return every ACE attached to ``path`` as a flat list in
            the order ``Get-Acl`` emitted them.  NTFS has no default
            ACL partition; the returned list is exactly
            :class:`WindowsAclInfo.aces`.

            :param path: The absolute path to query.

            :returns: A ``list[WindowsAceInfo]``.

            :raises: :class:`WindowsAclQueryError`
        """
        info = self.get_acl(path=path)

        rtnval: list = []
        for ace in info.aces:
            rtnval.append(ace)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_acl(
        self,
        path: str,
        acl_spec: list,
    ) -> WindowsAclActionResult:
        """
            Replace the full ACL on ``path`` with ``acl_spec``.

            Implemented as ``icacls '<path>' /reset`` followed by
            ``icacls '<path>' /grant[:r]`` (or ``/deny``) per ACE.
            The ``:r`` suffix on ``/grant`` requests "replace"
            semantics, which prevents merging with any inherited
            duplicates that survive ``/reset``.

            Skipped when the parsed current ACL matches `acl_spec`
            on the explicit (non-inherited) ACEs.

            :param path: The target path.
            :param acl_spec: A list of :class:`WindowsAceSpec`.

            :returns: The :class:`WindowsAclActionResult`.
        """
        reset_command = [self._icacls_executable(), path, _ICACLS_RESET]

        for spec in acl_spec:
            unknown_tokens = _find_unknown_rights_tokens(rights=spec.rights)
            if len(unknown_tokens) > 0:
                failed = self._build_result(
                    status=WindowsAclActionStatus.FAILED,
                    command=[],
                    path=path,
                    principal=spec.principal,
                    message=(
                        "unknown rights tokens supplied: {}".format(
                            ", ".join(unknown_tokens),
                        )
                    ),
                    stderr="invalid rights tokens",
                )
                return failed

        rendered_aces: list = []
        for spec in acl_spec:
            rendered_aces.append(_render_icacls_ace_arguments(spec=spec))

        if self._config.dry_run:
            preview_command = list(reset_command)
            for argv_tail in rendered_aces:
                preview_command.append("&&")
                preview_command.append(self._icacls_executable())
                preview_command.append(path)
                for token in argv_tail:
                    preview_command.append(token)
            dry = self._build_result(
                status=WindowsAclActionStatus.DRY_RUN,
                command=preview_command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except WindowsAclQueryError:
            current = None

        if current is not None and _acl_matches_spec(
            current=current,
            desired=acl_spec,
        ):
            skipped = self._build_result(
                status=WindowsAclActionStatus.SKIPPED,
                command=reset_command,
                path=path,
                message="path ACL already matches the requested spec",
            )
            return skipped

        reset_result = self._runner.run(
            list(reset_command),
            elevated=self._config.elevated,
        )
        if reset_result.exit_code != 0:
            failed = WindowsAclActionResult(
                status=WindowsAclActionStatus.FAILED,
                detail=path,
                exit_code=reset_result.exit_code,
                stdout=reset_result.stdout,
                stderr=reset_result.stderr,
                command=list(reset_command),
                path=path,
                message="icacls /reset failed before set",
            )
            return failed

        last_result: Optional[WindowsAclActionResult] = None
        for argv_tail in rendered_aces:
            command = [self._icacls_executable(), path]
            for token in argv_tail:
                command.append(token)
            step = self._dispatch_acl_write(
                command=command,
                path=path,
            )
            last_result = step
            if step.status == WindowsAclActionStatus.FAILED:
                return step

        if last_result is None:
            rtnval = WindowsAclActionResult(
                status=WindowsAclActionStatus.SUCCESS,
                detail=path,
                exit_code=reset_result.exit_code,
                stdout=reset_result.stdout,
                stderr=reset_result.stderr,
                command=list(reset_command),
                path=path,
            )
            return rtnval

        rtnval = last_result
        return rtnval

    def add_ace(
        self,
        path: str,
        principal: WindowsAceSpec,
        rights: Optional[list] = None,
    ) -> WindowsAclActionResult:
        """
            Add a single ACE to ``path``.

            Two calling shapes are accepted:

            * Pass a fully-populated :class:`WindowsAceSpec` as
              ``principal``.  ``rights`` MUST be ``None`` in that
              case.
            * Pass a :class:`WindowsAceSpec` whose ``rights`` is the
              empty list and a ``rights`` list argument.  The
              argument overrides the spec's ``rights`` field.

            Skipped when an ACE with the same principal /
            permission / rights / inheritance tuple is already
            present on the path's explicit (non-inherited) DACL.

            :param path: The target path.
            :param principal: The :class:`WindowsAceSpec` to add.
            :param rights: Optional override for ``principal.rights``.

            :returns: The :class:`WindowsAclActionResult`.
        """
        effective_spec = _materialise_ace_spec(
            principal=principal,
            rights_override=rights,
        )

        unknown_tokens = _find_unknown_rights_tokens(
            rights=effective_spec.rights,
        )
        if len(unknown_tokens) > 0:
            failed = self._build_result(
                status=WindowsAclActionStatus.FAILED,
                command=[],
                path=path,
                principal=effective_spec.principal,
                message=(
                    "unknown rights tokens supplied: {}".format(
                        ", ".join(unknown_tokens),
                    )
                ),
                stderr="invalid rights tokens",
            )
            return failed

        argv_tail = _render_icacls_ace_arguments(spec=effective_spec)
        command = [self._icacls_executable(), path]
        for token in argv_tail:
            command.append(token)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAclActionStatus.DRY_RUN,
                command=command,
                path=path,
                principal=effective_spec.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except WindowsAclQueryError:
            current = None

        if current is not None and _ace_already_present(
            current=current,
            spec=effective_spec,
        ):
            skipped = self._build_result(
                status=WindowsAclActionStatus.SKIPPED,
                command=command,
                path=path,
                principal=effective_spec.principal,
                message="ACE is already present on the target path",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
            principal=effective_spec.principal,
        )
        return rtnval

    def remove_ace(
        self,
        path: str,
        principal: WindowsAceSpec,
    ) -> WindowsAclActionResult:
        """
            Remove every ACE matching the ``principal`` (by name)
            from ``path``.  Implemented as ``icacls '<path>' /remove
            '<principal>'`` -- the ``/remove`` flag accepts only the
            principal and removes every matching grant / deny.

            Skipped when no explicit (non-inherited) ACE matches the
            principal.

            :param path: The target path.
            :param principal: The :class:`WindowsAceSpec`; only
                              ``principal`` is consulted for the
                              ``icacls`` argument.

            :returns: The :class:`WindowsAclActionResult`.
        """
        command = [
            self._icacls_executable(),
            path,
            _ICACLS_REMOVE,
            principal.principal,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAclActionStatus.DRY_RUN,
                command=command,
                path=path,
                principal=principal.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except WindowsAclQueryError:
            current = None

        if current is not None and not _ace_principal_present(
            current=current,
            principal=principal.principal,
        ):
            skipped = self._build_result(
                status=WindowsAclActionStatus.SKIPPED,
                command=command,
                path=path,
                principal=principal.principal,
                message="no matching explicit ACE to remove",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
            principal=principal.principal,
        )
        return rtnval

    def clear_acl(self, path: str) -> WindowsAclActionResult:
        """
            Reset the path's DACL to the inherited-only state via
            ``icacls '<path>' /reset``.  Skipped when the parsed
            DACL already contains only inherited ACEs.

            :param path: The target path.

            :returns: The :class:`WindowsAclActionResult`.
        """
        command = [self._icacls_executable(), path, _ICACLS_RESET]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAclActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except WindowsAclQueryError:
            current = None

        if current is not None and not _has_explicit_aces(info=current):
            skipped = self._build_result(
                status=WindowsAclActionStatus.SKIPPED,
                command=command,
                path=path,
                message="path has only inherited ACEs",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
        )
        return rtnval

    def copy_acl(
        self,
        src: str,
        dst: str,
    ) -> WindowsAclActionResult:
        """
            Copy the ACL from ``src`` to ``dst``.

            Implemented as a single PowerShell pipeline ``Get-Acl
            '<src>' | Set-Acl '<dst>'``.  This is the canonical
            NTFS-native form -- the ACL travels as a
            ``[FileSystemSecurity]`` object directly between
            cmdlets, so neither side needs to round-trip through a
            string serialisation.

            Skipped when the parsed source and destination SDDL
            strings are equal.

            :param src: Source path to copy from.
            :param dst: Destination path to copy onto.

            :returns: The :class:`WindowsAclActionResult`.
        """
        escaped_src = _ps_escape_single_quoted(value=src)
        escaped_dst = _ps_escape_single_quoted(value=dst)
        script = "Get-Acl -Path '{}' | Set-Acl -Path '{}'".format(
            escaped_src,
            escaped_dst,
        )
        command = self._build_powershell_argv(script=script)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsAclActionStatus.DRY_RUN,
                command=command,
                path=dst,
            )
            return dry

        try:
            src_info = self.get_acl(path=src)
        except WindowsAclQueryError as exc:
            failed = WindowsAclActionResult(
                status=WindowsAclActionStatus.FAILED,
                detail=src,
                exit_code=exc.exit_code,
                stdout=None,
                stderr=exc.stderr,
                command=list(exc.command),
                path=src,
                message="Get-Acl on source path failed",
            )
            return failed

        try:
            dst_info = self.get_acl(path=dst)
        except WindowsAclQueryError:
            dst_info = None

        if dst_info is not None and _acl_infos_equal(
            left=src_info,
            right=dst_info,
        ):
            skipped = self._build_result(
                status=WindowsAclActionStatus.SKIPPED,
                command=command,
                path=dst,
                message="destination ACL already matches source ACL",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=dst,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _powershell_executable(self) -> str:
        """
            Return the configured PowerShell executable.
        """
        config = self._config
        if isinstance(config, WindowsAclManagerConfig):
            rtnval = config.powershell_executable
        else:
            rtnval = _DEFAULT_POWERSHELL
        return rtnval

    def _icacls_executable(self) -> str:
        """
            Return the configured ``icacls`` binary path.
        """
        config = self._config
        if isinstance(config, WindowsAclManagerConfig):
            rtnval = config.icacls_executable
        else:
            rtnval = _ICACLS
        return rtnval

    def _build_powershell_argv(self, *, script: str) -> list:
        """
            Build the canonical ``["powershell.exe", "-NoProfile",
            "-NonInteractive", "-Command", "<script>"]`` argv.

            :param script: The PowerShell scriptblock body.
        """
        rtnval = [
            self._powershell_executable(),
            _PS_NOPROFILE,
            _PS_NONINTERACTIVE,
            _PS_COMMAND,
            script,
        ]
        return rtnval

    def _dispatch_acl_write(
        self,
        *,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
    ) -> WindowsAclActionResult:
        """
            Dispatch a write command and wrap the result with the
            ACL-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="acl_write",
            path=path,
            principal=principal,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsAclActionStatus,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsAclActionResult:
        """
            Construct a :class:`WindowsAclActionResult` without
            invoking the runner.
        """
        rtnval = WindowsAclActionResult(
            status=status,
            detail=path if path is not None else principal,
            command=list(command),
            path=path,
            principal=principal,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


# Mapping from ``[FileSystemRights]`` token -> ``icacls`` rights
# letter.  The list is intentionally narrow: ``icacls`` accepts a
# rich vocabulary of letter-coded rights, but only the canonical
# Windows host-management subset is rendered with the short letter
# form.  Long-form tokens not present in this map are passed through
# verbatim (``icacls`` accepts them under their parenthesised name)
# AFTER they pass the :data:`_KNOWN_RIGHTS_TOKENS` validation.
_RIGHTS_LETTER_MAP: dict = {
    "FullControl": "F",
    "Modify": "M",
    "ReadAndExecute": "RX",
    "Read": "R",
    "Write": "W",
}


# Canonical ``[FileSystemRights]`` vocabulary accepted by this manager.
# Mirrors the public enum surface of
# ``System.Security.AccessControl.FileSystemRights``.  Unknown tokens
# are rejected at the manager boundary with a ``FAILED`` result so
# callers learn about invalid rights without an opaque ``icacls``
# stderr message.
_KNOWN_RIGHTS_TOKENS = frozenset({
    "FullControl",
    "Modify",
    "ReadAndExecute",
    "ListDirectory",
    "Read",
    "Write",
    "ReadData",
    "WriteData",
    "AppendData",
    "Delete",
    "DeleteSubdirectoriesAndFiles",
    "ChangePermissions",
    "TakeOwnership",
    "Synchronize",
    "ExecuteFile",
    "ReadAttributes",
    "WriteAttributes",
    "ReadExtendedAttributes",
    "WriteExtendedAttributes",
    "ReadPermissions",
    "Traverse",
})


def _ps_escape_single_quoted(*, value: str) -> str:
    """
        Escape a string for inclusion inside PowerShell single
        quotes.  PowerShell single-quoted strings are literal except
        for the single quote itself, which is escaped by doubling it.

        :param value: The raw string.
    """
    rtnval = value.replace("'", "''")
    return rtnval


def _materialise_ace_spec(
    *,
    principal: WindowsAceSpec,
    rights_override: Optional[list],
) -> WindowsAceSpec:
    """
        Return a normalised :class:`WindowsAceSpec`, applying any
        ``rights_override`` from the caller.  We never mutate the
        caller's spec.

        :param principal: The caller-supplied spec.
        :param rights_override: Optional overriding rights list.
    """
    if rights_override is not None:
        effective_rights = list(rights_override)
    else:
        effective_rights = list(principal.rights)

    rtnval = WindowsAceSpec(
        principal=principal.principal,
        permission=principal.permission,
        rights=effective_rights,
        inheritance=principal.inheritance,
    )
    return rtnval


def _find_unknown_rights_tokens(*, rights: list) -> list:
    """
        Return the list of rights tokens not present in
        :data:`_KNOWN_RIGHTS_TOKENS`.  An empty list means every token
        is valid.

        :param rights: The rights list from the caller.
    """
    rtnval: list = []
    for token in rights:
        if token not in _KNOWN_RIGHTS_TOKENS:
            rtnval.append(token)
    return rtnval


def _render_icacls_rights_token(*, rights: list) -> str:
    """
        Render a rights list into the parenthesised ``icacls``
        argument body (e.g. ``"(M,W)"``, ``"(F)"``).

        Each token is mapped through :data:`_RIGHTS_LETTER_MAP` when
        it has a canonical short letter; unmapped tokens are passed
        through verbatim, which ``icacls`` accepts for the long-form
        token list.

        :param rights: The list of rights tokens.
    """
    mapped: list = []
    for token in rights:
        if token in _RIGHTS_LETTER_MAP:
            mapped.append(_RIGHTS_LETTER_MAP[token])
        else:
            mapped.append(token)

    body_text = ",".join(mapped)
    rtnval = "({})".format(body_text)
    return rtnval


def _render_icacls_inheritance_prefix(
    *,
    inheritance: Optional[WindowsAceInheritance],
) -> str:
    """
        Render the inheritance flag prefix that prepends the rights
        body in ``icacls`` arguments (e.g. ``"(OI)(CI)"``).

        :param inheritance: The inheritance flag set, or ``None``.
    """
    if inheritance is None or inheritance == WindowsAceInheritance.NONE:
        rtnval = ""
        return rtnval

    if inheritance == WindowsAceInheritance.OBJECT_INHERIT:
        rtnval = "(OI)"
        return rtnval

    if inheritance == WindowsAceInheritance.CONTAINER_INHERIT:
        rtnval = "(CI)"
        return rtnval

    if inheritance == WindowsAceInheritance.BOTH:
        rtnval = "(OI)(CI)"
        return rtnval

    err_msg = "Unknown WindowsAceInheritance: {}".format(inheritance)
    raise ValueError(err_msg)


def _render_icacls_ace_arguments(*, spec: WindowsAceSpec) -> list:
    """
        Render a :class:`WindowsAceSpec` into the icacls argv tail
        (the part that follows the path argument).

        Examples::

            WindowsAceSpec("DOMAIN\\alice", ALLOW, ["Modify"])
                -> ["/grant:r", "DOMAIN\\alice:(M)"]

            WindowsAceSpec("BUILTIN\\Users", DENY, ["Write"])
                -> ["/deny", "BUILTIN\\Users:(W)"]

            WindowsAceSpec("DOMAIN\\alice", ALLOW, ["Modify"],
                           inheritance=BOTH)
                -> ["/grant:r", "DOMAIN\\alice:(OI)(CI)(M)"]

        :param spec: The ACE.

        :returns: The icacls argv tail, in order.
    """
    rights_body = _render_icacls_rights_token(rights=spec.rights)
    inheritance_prefix = _render_icacls_inheritance_prefix(
        inheritance=spec.inheritance,
    )
    principal_token = "{}:{}{}".format(
        spec.principal,
        inheritance_prefix,
        rights_body,
    )

    if spec.permission == WindowsAcePermission.ALLOW:
        verb = _ICACLS_GRANT_REPLACE
    elif spec.permission == WindowsAcePermission.DENY:
        verb = _ICACLS_DENY
    else:
        err_msg = "Unknown WindowsAcePermission: {}".format(spec.permission)
        raise ValueError(err_msg)

    rtnval = [verb, principal_token]
    return rtnval


def _parse_get_acl_payload(
    *,
    output: str,
    path: str,
) -> WindowsAclInfo:
    """
        Parse the JSON output of the ``Get-Acl`` PowerShell
        scriptblock dispatched by :meth:`WindowsAclManager.get_acl`.

        The expected payload is a single object with the keys
        ``Owner``, ``Group``, ``Sddl``, and ``Access``.  ``Access``
        is either a list of dicts (when the path has multiple ACEs)
        or a single dict (when it has exactly one); both shapes are
        normalised.

        :param output: The captured stdout.
        :param path: The path the read was for.

        :returns: The parsed :class:`WindowsAclInfo`.
    """
    info = WindowsAclInfo(path=path)

    text = output.strip()
    if len(text) == 0:
        rtnval = info
        return rtnval

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        rtnval = info
        return rtnval

    if not isinstance(parsed, dict):
        rtnval = info
        return rtnval

    owner_raw = parsed.get("Owner")
    if owner_raw is not None and len(str(owner_raw)) > 0:
        info.owner = str(owner_raw)

    group_raw = parsed.get("Group")
    if group_raw is not None and len(str(group_raw)) > 0:
        info.group = str(group_raw)

    sddl_raw = parsed.get("Sddl")
    if sddl_raw is not None and len(str(sddl_raw)) > 0:
        info.sddl = str(sddl_raw)

    access_raw = parsed.get("Access")
    access_records = _normalize_access_records(raw=access_raw)
    for record in access_records:
        ace = _parse_ace_record(record=record)
        if ace is None:
            continue
        info.aces.append(ace)

    rtnval = info
    return rtnval


def _normalize_access_records(*, raw) -> list:
    """
        Normalise ``Access`` into a list of dicts.  ``ConvertTo-Json``
        emits a bare object when there is exactly one record; a list
        otherwise.

        :param raw: The raw ``Access`` field value.
    """
    if raw is None:
        rtnval: list = []
        return rtnval

    if isinstance(raw, list):
        rtnval = []
        for entry in raw:
            if isinstance(entry, dict):
                rtnval.append(entry)
        return rtnval

    if isinstance(raw, dict):
        rtnval = [raw]
        return rtnval

    rtnval = []
    return rtnval


def _parse_ace_record(*, record: dict) -> Optional[WindowsAceInfo]:
    """
        Parse a single ``Get-Acl`` ``Access`` JSON record into a
        :class:`WindowsAceInfo`.

        Expected shape::

            {
                "FileSystemRights": "Modify, Synchronize",
                "AccessControlType": 0,
                "IdentityReference": {"Value": "DOMAIN\\alice"} or
                                      "DOMAIN\\alice",
                "IsInherited": false,
                "InheritanceFlags": 3,
                "PropagationFlags": 0
            }

        ``AccessControlType`` is ``0`` for Allow and ``1`` for Deny
        when ConvertTo-Json renders the enum as an integer; some
        PowerShell builds render it as the string ``"Allow"`` or
        ``"Deny"``.  Both shapes are accepted.

        :param record: The raw ACE record.
    """
    identity_raw = record.get("IdentityReference")
    if isinstance(identity_raw, dict):
        principal_value: Optional[str] = identity_raw.get("Value")
    elif isinstance(identity_raw, str):
        principal_value = identity_raw
    else:
        principal_value = None

    if principal_value is None or len(principal_value) == 0:
        rtnval: Optional[WindowsAceInfo] = None
        return rtnval

    permission_raw = record.get("AccessControlType")
    if isinstance(permission_raw, str):
        if permission_raw == "Allow":
            permission_value = WindowsAcePermission.ALLOW
        elif permission_raw == "Deny":
            permission_value = WindowsAcePermission.DENY
        else:
            permission_value = WindowsAcePermission.ALLOW
    elif isinstance(permission_raw, int):
        if permission_raw == 0:
            permission_value = WindowsAcePermission.ALLOW
        else:
            permission_value = WindowsAcePermission.DENY
    else:
        permission_value = WindowsAcePermission.ALLOW

    rights_raw = record.get("FileSystemRights")
    if isinstance(rights_raw, str):
        rights_text = rights_raw
    elif isinstance(rights_raw, int):
        # Rare: ConvertTo-Json sometimes emits the underlying enum
        # value as an integer.  We leave these as the integer-as-
        # string in the rights list so callers can still inspect
        # them; idempotency comparisons across mixed shapes are
        # therefore best-effort.
        rights_text = str(rights_raw)
    else:
        rights_text = ""

    rights_list: list = []
    for token in rights_text.split(","):
        cleaned = token.strip()
        if len(cleaned) == 0:
            continue
        rights_list.append(cleaned)

    inherited_raw = record.get("IsInherited")
    if isinstance(inherited_raw, bool):
        inherited_flag = inherited_raw
    elif isinstance(inherited_raw, str):
        inherited_flag = inherited_raw.lower() in ("true", "1")
    else:
        inherited_flag = False

    inheritance_flags_raw = record.get("InheritanceFlags")
    inheritance_value = _coerce_inheritance(raw=inheritance_flags_raw)

    rtnval = WindowsAceInfo(
        principal=principal_value,
        permission=permission_value,
        rights=rights_list,
        inheritance=inheritance_value,
        is_inherited=inherited_flag,
    )
    return rtnval


def _coerce_inheritance(*, raw) -> WindowsAceInheritance:
    """
        Coerce a raw ``InheritanceFlags`` field into a
        :class:`WindowsAceInheritance` value.

        ``System.Security.AccessControl.InheritanceFlags`` is a
        ``[Flags]`` enum: ``None=0``, ``ContainerInherit=1``,
        ``ObjectInherit=2``.

        :param raw: The raw field value.
    """
    if isinstance(raw, int):
        if raw == 0:
            rtnval = WindowsAceInheritance.NONE
            return rtnval
        if raw == 1:
            rtnval = WindowsAceInheritance.CONTAINER_INHERIT
            return rtnval
        if raw == 2:
            rtnval = WindowsAceInheritance.OBJECT_INHERIT
            return rtnval
        if raw == 3:
            rtnval = WindowsAceInheritance.BOTH
            return rtnval

    if isinstance(raw, str):
        text = raw.strip()
        if text == "None" or len(text) == 0:
            rtnval = WindowsAceInheritance.NONE
            return rtnval
        if text == "ContainerInherit":
            rtnval = WindowsAceInheritance.CONTAINER_INHERIT
            return rtnval
        if text == "ObjectInherit":
            rtnval = WindowsAceInheritance.OBJECT_INHERIT
            return rtnval
        # The flags-style serialization joins multiple flags with
        # ", ".
        if "ContainerInherit" in text and "ObjectInherit" in text:
            rtnval = WindowsAceInheritance.BOTH
            return rtnval
        if "ContainerInherit" in text:
            rtnval = WindowsAceInheritance.CONTAINER_INHERIT
            return rtnval
        if "ObjectInherit" in text:
            rtnval = WindowsAceInheritance.OBJECT_INHERIT
            return rtnval

    rtnval = WindowsAceInheritance.NONE
    return rtnval


def _ace_already_present(
    *,
    current: WindowsAclInfo,
    spec: WindowsAceSpec,
) -> bool:
    """
        Return ``True`` when ``current`` contains an explicit
        (non-inherited) ACE matching ``spec`` on principal,
        permission, rights-set (order-insensitive), and inheritance.

        :param current: The current :class:`WindowsAclInfo`.
        :param spec: The desired ACE.
    """
    spec_rights_set = frozenset(spec.rights)
    if spec.inheritance is None:
        spec_inheritance = WindowsAceInheritance.NONE
    else:
        spec_inheritance = spec.inheritance

    rtnval = False
    for ace in current.aces:
        if ace.is_inherited:
            continue
        if ace.principal != spec.principal:
            continue
        if ace.permission != spec.permission:
            continue
        ace_rights_set = frozenset(ace.rights)
        if ace_rights_set != spec_rights_set:
            continue
        if ace.inheritance != spec_inheritance:
            continue
        rtnval = True
        break
    return rtnval


def _ace_principal_present(
    *,
    current: WindowsAclInfo,
    principal: str,
) -> bool:
    """
        Return ``True`` when the parsed ACL contains an explicit
        (non-inherited) ACE for the given principal, regardless of
        rights / permission.

        :param current: The current :class:`WindowsAclInfo`.
        :param principal: The principal whose presence we are
                          testing.
    """
    rtnval = False
    for ace in current.aces:
        if ace.is_inherited:
            continue
        if ace.principal != principal:
            continue
        rtnval = True
        break
    return rtnval


def _has_explicit_aces(*, info: WindowsAclInfo) -> bool:
    """
        Return ``True`` when ``info`` contains at least one explicit
        (non-inherited) ACE.

        :param info: The parsed ACL.
    """
    rtnval = False
    for ace in info.aces:
        if ace.is_inherited:
            continue
        rtnval = True
        break
    return rtnval


def _acl_matches_spec(
    *,
    current: WindowsAclInfo,
    desired: list,
) -> bool:
    """
        Return ``True`` when the parsed explicit ACL is tuple-equal
        to the requested ``desired`` list.

        :param current: The current :class:`WindowsAclInfo`.
        :param desired: A list of :class:`WindowsAceSpec`.
    """
    desired_tuples: list = []
    for spec in desired:
        if spec.inheritance is None:
            spec_inheritance = WindowsAceInheritance.NONE
        else:
            spec_inheritance = spec.inheritance
        normalised = (
            spec.principal,
            spec.permission,
            frozenset(spec.rights),
            spec_inheritance,
        )
        desired_tuples.append(normalised)

    current_tuples: list = []
    for ace in current.aces:
        if ace.is_inherited:
            continue
        normalised = (
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
            ace.inheritance,
        )
        current_tuples.append(normalised)

    desired_sorted = sorted(desired_tuples, key=_ace_sort_key)
    current_sorted = sorted(current_tuples, key=_ace_sort_key)

    rtnval = desired_sorted == current_sorted
    return rtnval


def _ace_sort_key(item) -> tuple:
    """
        Stable sort key for an ACE comparison tuple
        ``(principal, permission, rights_set, inheritance)``.

        :param item: The 4-tuple.
    """
    principal_value, permission_value, rights_set, inheritance_value = item
    rights_text = ",".join(sorted(rights_set))
    rtnval = (
        principal_value,
        permission_value.value,
        rights_text,
        inheritance_value.value,
    )
    return rtnval


def _acl_infos_equal(
    *,
    left: WindowsAclInfo,
    right: WindowsAclInfo,
) -> bool:
    """
        Return ``True`` when two :class:`WindowsAclInfo` snapshots
        are equal.

        We compare on SDDL when both sides have a populated SDDL
        string (the canonical NTFS round-trip form).  Otherwise we
        fall back to comparing the explicit ACE list as a sorted
        tuple.

        :param left: One snapshot.
        :param right: The other snapshot.
    """
    if (
        left.sddl is not None
        and right.sddl is not None
        and len(left.sddl) > 0
        and len(right.sddl) > 0
    ):
        rtnval = left.sddl == right.sddl
        return rtnval

    left_tuples: list = []
    for ace in left.aces:
        if ace.is_inherited:
            continue
        normalised = (
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
            ace.inheritance,
        )
        left_tuples.append(normalised)

    right_tuples: list = []
    for ace in right.aces:
        if ace.is_inherited:
            continue
        normalised = (
            ace.principal,
            ace.permission,
            frozenset(ace.rights),
            ace.inheritance,
        )
        right_tuples.append(normalised)

    left_sorted = sorted(left_tuples, key=_ace_sort_key)
    right_sorted = sorted(right_tuples, key=_ace_sort_key)

    rtnval = left_sorted == right_sorted
    return rtnval
