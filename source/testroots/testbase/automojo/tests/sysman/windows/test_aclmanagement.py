"""
    Tests for :class:`WindowsAclManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell / ``icacls`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.aclmanagement import (
    WindowsAceInheritance,
    WindowsAcePermission,
    WindowsAceSpec,
    WindowsAclActionStatus,
    WindowsAclManager,
    WindowsAclManagerConfig,
    WindowsAclQueryError,
)


_PS_PREFIX = ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command")


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _build_get_acl_script(*, path: str) -> str:
    """
        Build the canonical ``Get-Acl`` PowerShell scriptblock the
        manager dispatches.  Single-quoted paths and the
        ``Owner / Group / Sddl / Access`` projection match what
        :meth:`WindowsAclManager.get_acl` constructs.
    """
    rtnval = (
        "$acl = Get-Acl -Path '{}'; "
        "$payload = [PSCustomObject]@{{"
        "Owner = $acl.Owner; "
        "Group = $acl.Group; "
        "Sddl = $acl.Sddl; "
        "Access = $acl.Access"
        "}}; "
        "$payload | ConvertTo-Json -Depth 5 -Compress"
    ).format(path)
    return rtnval


def _ps_argv(*, script: str) -> tuple:
    """
        Build the canonical PowerShell argv tuple for a scriptblock so
        tests can key the mock runner.
    """
    rtnval = _PS_PREFIX + (script,)
    return rtnval


def _full_acl_payload() -> str:
    """
        Build a representative ``Get-Acl | ConvertTo-Json`` payload for
        a directory carrying three explicit ACEs (one allow user, one
        allow group, one deny user) plus one inherited ACE.

        ``Access`` is the multi-element-array form.
    """
    rtnval = (
        '{'
        '"Owner":"BUILTIN\\\\Administrators",'
        '"Group":"DOMAIN\\\\Domain Users",'
        '"Sddl":"O:BAG:DUD:PAI(A;;FA;;;BA)(A;;0x1200a9;;;BU)(D;;DCLCRPCR;;;S-1-1-0)(A;ID;0x1200a9;;;SY)",'
        '"Access":['
        '{"FileSystemRights":"FullControl","AccessControlType":0,'
        '"IdentityReference":{"Value":"BUILTIN\\\\Administrators"},'
        '"IsInherited":false,"InheritanceFlags":3,"PropagationFlags":0},'
        '{"FileSystemRights":"ReadAndExecute, Synchronize","AccessControlType":0,'
        '"IdentityReference":{"Value":"BUILTIN\\\\Users"},'
        '"IsInherited":false,"InheritanceFlags":3,"PropagationFlags":0},'
        '{"FileSystemRights":"Modify, Synchronize","AccessControlType":1,'
        '"IdentityReference":{"Value":"Everyone"},'
        '"IsInherited":false,"InheritanceFlags":0,"PropagationFlags":0},'
        '{"FileSystemRights":"ReadAndExecute, Synchronize","AccessControlType":0,'
        '"IdentityReference":{"Value":"NT AUTHORITY\\\\SYSTEM"},'
        '"IsInherited":true,"InheritanceFlags":3,"PropagationFlags":0}'
        ']'
        '}'
    )
    return rtnval


def _single_ace_payload() -> str:
    """
        Build a ``Get-Acl | ConvertTo-Json`` payload where ``Access``
        is a single bare object (the ConvertTo-Json single-element
        quirk).
    """
    rtnval = (
        '{'
        '"Owner":"BUILTIN\\\\Administrators",'
        '"Group":null,'
        '"Sddl":"O:BAG:DUD:PAI(A;;FA;;;BA)",'
        '"Access":'
        '{"FileSystemRights":"FullControl","AccessControlType":0,'
        '"IdentityReference":{"Value":"BUILTIN\\\\Administrators"},'
        '"IsInherited":false,"InheritanceFlags":3,"PropagationFlags":0}'
        '}'
    )
    return rtnval


def _minimal_acl_payload() -> str:
    """
        Build a ``Get-Acl | ConvertTo-Json`` payload where the path
        carries no explicit ACEs (only an inherited one).  ``set_acl``
        and ``add_ace`` against this should NOT short-circuit on
        idempotency for any caller-supplied explicit ACE.
    """
    rtnval = (
        '{'
        '"Owner":"BUILTIN\\\\Administrators",'
        '"Group":null,'
        '"Sddl":"O:BAG:DUD:AI(A;ID;0x1200a9;;;BU)",'
        '"Access":['
        '{"FileSystemRights":"ReadAndExecute, Synchronize","AccessControlType":0,'
        '"IdentityReference":{"Value":"BUILTIN\\\\Users"},'
        '"IsInherited":true,"InheritanceFlags":3,"PropagationFlags":0}'
        ']'
        '}'
    )
    return rtnval


def _seed_full_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed the ``Get-Acl`` PowerShell scriptblock for ``path`` to
        return the full ACL payload.
    """
    runner.results[_ps_argv(script=_build_get_acl_script(path=path))] = (
        RunResult(
            exit_code=0,
            stdout=_full_acl_payload(),
            stderr="",
        )
    )
    return


def _seed_single_ace_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed the ``Get-Acl`` PowerShell scriptblock for ``path`` to
        return the single-ACE bare-object payload.
    """
    runner.results[_ps_argv(script=_build_get_acl_script(path=path))] = (
        RunResult(
            exit_code=0,
            stdout=_single_ace_payload(),
            stderr="",
        )
    )
    return


def _seed_minimal_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed the ``Get-Acl`` PowerShell scriptblock for ``path`` to
        return a payload with no explicit ACEs.
    """
    runner.results[_ps_argv(script=_build_get_acl_script(path=path))] = (
        RunResult(
            exit_code=0,
            stdout=_minimal_acl_payload(),
            stderr="",
        )
    )
    return


# ----------------------------------------------------------------------
# get_acl / list_aces
# ----------------------------------------------------------------------


def test_get_acl_parses_powershell_json():
    """
        get_acl returns a WindowsAclInfo populated from the parsed
        Get-Acl ConvertTo-Json output.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(),
    )
    info = manager.get_acl(path="C:\\Data")

    testbase.assert_equal(info.path, "C:\\Data")
    testbase.assert_equal(info.owner, "BUILTIN\\Administrators")
    testbase.assert_equal(info.group, "DOMAIN\\Domain Users")
    testbase.assert_equal(len(info.aces), 4)

    first = info.aces[0]
    testbase.assert_equal(first.principal, "BUILTIN\\Administrators")
    testbase.assert_equal(first.permission, WindowsAcePermission.ALLOW)
    testbase.assert_equal(first.rights, ["FullControl"])
    testbase.assert_equal(first.inheritance, WindowsAceInheritance.BOTH)
    testbase.assert_equal(first.is_inherited, False)

    deny_ace = info.aces[2]
    testbase.assert_equal(deny_ace.permission, WindowsAcePermission.DENY)
    testbase.assert_equal(deny_ace.principal, "Everyone")
    testbase.assert_equal(
        deny_ace.rights, ["Modify", "Synchronize"],
    )
    testbase.assert_equal(deny_ace.inheritance, WindowsAceInheritance.NONE)

    inherited_ace = info.aces[3]
    testbase.assert_equal(inherited_ace.is_inherited, True)
    testbase.assert_equal(
        inherited_ace.principal, "NT AUTHORITY\\SYSTEM",
    )
    return


def test_get_acl_parses_single_ace_object_form():
    """
        get_acl accepts the bare-object form ConvertTo-Json emits when
        ``Access`` has exactly one record.
    """
    runner = _MockCommandRunner()
    _seed_single_ace_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(),
    )
    info = manager.get_acl(path="C:\\Data")

    testbase.assert_equal(len(info.aces), 1)
    only_ace = info.aces[0]
    testbase.assert_equal(only_ace.principal, "BUILTIN\\Administrators")
    testbase.assert_equal(only_ace.permission, WindowsAcePermission.ALLOW)
    testbase.assert_equal(only_ace.rights, ["FullControl"])
    return


def test_list_aces_returns_full_set():
    """
        list_aces returns all parsed ACEs as a flat list (including
        inherited ones).
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(),
    )
    aces = manager.list_aces(path="C:\\Data")

    testbase.assert_equal(len(aces), 4)
    return


# ----------------------------------------------------------------------
# set_acl
# ----------------------------------------------------------------------


def test_set_acl_invokes_set_acl():
    """
        set_acl dispatches icacls /reset followed by icacls /grant:r
        per ACE.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = [
        WindowsAceSpec(
            principal="DOMAIN\\alice",
            permission=WindowsAcePermission.ALLOW,
            rights=["Modify"],
        ),
        WindowsAceSpec(
            principal="BUILTIN\\Users",
            permission=WindowsAcePermission.ALLOW,
            rights=["ReadAndExecute"],
        ),
    ]
    result = manager.set_acl(path="C:\\Data", acl_spec=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SUCCESS)

    icacls_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    # One /reset, two /grant:r writes.
    testbase.assert_equal(len(icacls_calls), 3)
    testbase.assert_equal(
        icacls_calls[0]["args"],
        ["icacls.exe", "C:\\Data", "/reset"],
    )
    testbase.assert_equal(
        icacls_calls[1]["args"],
        ["icacls.exe", "C:\\Data", "/grant:r", "DOMAIN\\alice:(M)"],
    )
    testbase.assert_equal(
        icacls_calls[2]["args"],
        ["icacls.exe", "C:\\Data", "/grant:r", "BUILTIN\\Users:(RX)"],
    )
    for call in icacls_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_set_acl_skipped_when_match():
    """
        set_acl returns SKIPPED when the parsed explicit ACL already
        matches the requested spec.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = [
        WindowsAceSpec(
            principal="BUILTIN\\Administrators",
            permission=WindowsAcePermission.ALLOW,
            rights=["FullControl"],
            inheritance=WindowsAceInheritance.BOTH,
        ),
        WindowsAceSpec(
            principal="BUILTIN\\Users",
            permission=WindowsAcePermission.ALLOW,
            rights=["ReadAndExecute", "Synchronize"],
            inheritance=WindowsAceInheritance.BOTH,
        ),
        WindowsAceSpec(
            principal="Everyone",
            permission=WindowsAcePermission.DENY,
            rights=["Modify", "Synchronize"],
            inheritance=WindowsAceInheritance.NONE,
        ),
    ]
    result = manager.set_acl(path="C:\\Data", acl_spec=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SKIPPED)
    icacls_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    testbase.assert_equal(icacls_calls, [])
    return


def test_set_acl_dry_run_short_circuits():
    """
        set_acl returns DRY_RUN without dispatching any icacls or
        Get-Acl call when config.dry_run is True.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(dry_run=True, elevated=True),
    )
    spec = [
        WindowsAceSpec(
            principal="DOMAIN\\alice",
            permission=WindowsAcePermission.ALLOW,
            rights=["Read"],
        ),
    ]
    result = manager.set_acl(path="C:\\Data", acl_spec=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# add_ace
# ----------------------------------------------------------------------


def test_add_ace_invokes_powershell():
    """
        add_ace dispatches one icacls /grant:r call with the rendered
        principal:rights token.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="DOMAIN\\charlie",
        permission=WindowsAcePermission.ALLOW,
        rights=["Modify"],
    )
    result = manager.add_ace(path="C:\\Data", principal=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "icacls.exe" and "/grant:r" in c["args"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["icacls.exe", "C:\\Data", "/grant:r", "DOMAIN\\charlie:(M)"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_add_ace_skipped_when_present():
    """
        add_ace returns SKIPPED when the same ACE is already present
        on the explicit DACL.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="BUILTIN\\Administrators",
        permission=WindowsAcePermission.ALLOW,
        rights=["FullControl"],
        inheritance=WindowsAceInheritance.BOTH,
    )
    result = manager.add_ace(path="C:\\Data", principal=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_add_ace_with_inheritance_flags():
    """
        add_ace renders the inheritance flag prefix (OI)(CI) into the
        icacls argument when WindowsAceInheritance.BOTH is set.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="DOMAIN\\alice",
        permission=WindowsAcePermission.ALLOW,
        rights=["Modify"],
        inheritance=WindowsAceInheritance.BOTH,
    )
    manager.add_ace(path="C:\\Data", principal=spec)

    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "icacls.exe" and "/grant:r" in c["args"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        [
            "icacls.exe",
            "C:\\Data",
            "/grant:r",
            "DOMAIN\\alice:(OI)(CI)(M)",
        ],
    )
    return


# ----------------------------------------------------------------------
# remove_ace
# ----------------------------------------------------------------------


def test_remove_ace_invokes_icacls():
    """
        remove_ace dispatches icacls /remove '<principal>'.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="BUILTIN\\Administrators",
        permission=WindowsAcePermission.ALLOW,
        rights=[],
    )
    result = manager.remove_ace(path="C:\\Data", principal=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SUCCESS)
    remove_calls = [
        c for c in runner.calls
        if c["args"][0] == "icacls.exe" and "/remove" in c["args"]
    ]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(
        remove_calls[0]["args"],
        ["icacls.exe", "C:\\Data", "/remove", "BUILTIN\\Administrators"],
    )
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_remove_ace_skipped_when_absent():
    """
        remove_ace returns SKIPPED when no explicit ACE matches the
        principal.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="DOMAIN\\ghost",
        permission=WindowsAcePermission.ALLOW,
        rights=[],
    )
    result = manager.remove_ace(path="C:\\Data", principal=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.SKIPPED)
    remove_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# clear_acl
# ----------------------------------------------------------------------


def test_clear_acl_invokes_icacls_reset():
    """
        clear_acl dispatches icacls /reset when explicit ACEs exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    result = manager.clear_acl(path="C:\\Data")

    testbase.assert_equal(result.status, WindowsAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["icacls.exe", "C:\\Data", "/reset"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# copy_acl
# ----------------------------------------------------------------------


def test_copy_acl_invokes_pipeline():
    """
        copy_acl dispatches the Get-Acl | Set-Acl PowerShell pipeline.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "C:\\Source")
    _seed_minimal_acl(runner, "C:\\Dest")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="C:\\Source", dst="C:\\Dest")

    testbase.assert_equal(result.status, WindowsAclActionStatus.SUCCESS)

    pipeline_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "Get-Acl" in c["args"][-1]
        and "Set-Acl" in c["args"][-1]
    ]
    testbase.assert_equal(len(pipeline_calls), 1)
    testbase.assert_equal(pipeline_calls[0]["elevated"], True)
    pipeline_script = pipeline_calls[0]["args"][-1]
    testbase.assert_equal(
        pipeline_script,
        "Get-Acl -Path 'C:\\Source' | Set-Acl -Path 'C:\\Dest'",
    )
    return


def test_copy_acl_skipped_when_match():
    """
        copy_acl returns SKIPPED when the parsed source and destination
        SDDL strings already match.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Source")
    _seed_full_acl(runner, "C:\\Dest")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="C:\\Source", dst="C:\\Dest")

    testbase.assert_equal(result.status, WindowsAclActionStatus.SKIPPED)
    pipeline_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "Get-Acl" in c["args"][-1]
        and "Set-Acl" in c["args"][-1]
    ]
    testbase.assert_equal(pipeline_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation, error surface, contract corners
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes elevated=True to the runner when the
        manager is configured with elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    manager.add_ace(
        path="C:\\Data",
        principal=WindowsAceSpec(
            principal="DOMAIN\\alice",
            permission=WindowsAcePermission.ALLOW,
            rights=["Read"],
        ),
    )

    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "icacls.exe" and "/grant:r" in c["args"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Every read verb passes elevated=False to the runner regardless
        of config.elevated.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    manager.get_acl(path="C:\\Data")
    manager.list_aces(path="C:\\Data")

    read_calls = [
        c for c in runner.calls
        if c["args"][0] == "powershell.exe"
        and "Get-Acl" in c["args"][-1]
    ]
    testbase.assert_not_equal(len(read_calls), 0)
    for call in read_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write surfaces FAILED with the runner's
        captured stderr / exit_code on the result.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "C:\\Data")
    failure = RunResult(
        exit_code=5,
        stdout="",
        stderr="Access is denied.",
    )
    runner.results[(
        "icacls.exe",
        "C:\\Data",
        "/grant:r",
        "DOMAIN\\alice:(R)",
    )] = failure

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=False),
    )
    result = manager.add_ace(
        path="C:\\Data",
        principal=WindowsAceSpec(
            principal="DOMAIN\\alice",
            permission=WindowsAcePermission.ALLOW,
            rights=["Read"],
        ),
    )

    testbase.assert_equal(result.status, WindowsAclActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 5)
    testbase.assert_equal(result.stderr, "Access is denied.")
    return


def test_get_acl_raises_on_failure():
    """
        get_acl raises WindowsAclQueryError when Get-Acl exits
        non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[
        _ps_argv(script=_build_get_acl_script(path="C:\\Missing"))
    ] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Get-Acl : Cannot find path 'C:\\Missing'",
    )

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(),
    )
    raised = False
    try:
        manager.get_acl(path="C:\\Missing")
    except WindowsAclQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_unknown_rights_token_returns_failed():
    """
        add_ace returns FAILED with a descriptive message when the
        spec carries a rights token outside the canonical
        [FileSystemRights] vocabulary.  No icacls call is dispatched.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "C:\\Data")

    manager = WindowsAclManager(
        runner=runner,
        config=WindowsAclManagerConfig(elevated=True),
    )
    spec = WindowsAceSpec(
        principal="DOMAIN\\alice",
        permission=WindowsAcePermission.ALLOW,
        rights=["NotARealRight"],
    )
    result = manager.add_ace(path="C:\\Data", principal=spec)

    testbase.assert_equal(result.status, WindowsAclActionStatus.FAILED)
    icacls_calls = [
        c for c in runner.calls if c["args"][0] == "icacls.exe"
    ]
    testbase.assert_equal(icacls_calls, [])
    return
