"""
    Tests for :class:`WindowsIdentityManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live PowerShell / RSAT cmdlets are invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.windows.identitymanagement import (
    WindowsIdentityActionStatus,
    WindowsIdentityManager,
    WindowsIdentityManagerConfig,
    WindowsIdentityQueryError,
    WindowsRealmJoinSpec,
    WindowsRealmKind,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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


def _seed_runner_by_substring(
    runner: _MockCommandRunner,
    *,
    needle: str,
    result: RunResult,
) -> None:
    """
        Pre-seed a result that fires when the scriptblock argv slot
        contains ``needle`` as a substring.  Implemented by inspecting
        every recorded call from a custom ``run`` wrapper.
    """
    # The mock's results dict only matches exact argv tuples, so this
    # helper sets up a side-table that the wrapped ``run`` consults.
    if not hasattr(runner, "_substring_results"):
        runner._substring_results = []
    runner._substring_results.append((needle, result))

    original_run = runner.run

    def patched_run(args, **kwargs):
        for needle_value, seeded in runner._substring_results:
            if len(args) > 0 and needle_value in args[-1]:
                runner.calls.append(
                    {"args": list(args), "elevated": kwargs.get("elevated", False), "stdin": kwargs.get("stdin")},
                )
                return seeded
        return original_run(args, **kwargs)

    runner.run = patched_run
    return


# ----------------------------------------------------------------------
# Canonical PowerShell output samples
# ----------------------------------------------------------------------


_WMI_DOMAIN_JOINED = json.dumps({
    "Domain": "ad.example.com",
    "PartOfDomain": True,
    "DomainRole": 3,
    "Name": "WIN-HOST",
})


_WMI_WORKGROUP = json.dumps({
    "Domain": "WORKGROUP",
    "PartOfDomain": False,
    "DomainRole": 0,
    "Name": "WIN-HOST",
})


_GET_ADUSER_RECORD = json.dumps({
    "SamAccountName": "alice",
    "DistinguishedName": "CN=alice,OU=Users,DC=ad,DC=example,DC=com",
    "UserPrincipalName": "alice@ad.example.com",
    "MemberOf": [
        "CN=Domain Admins,OU=Groups,DC=ad,DC=example,DC=com",
        "CN=Engineers,OU=Groups,DC=ad,DC=example,DC=com",
    ],
    "HomeDirectory": "\\\\fileserver\\home\\alice",
    "uidNumber": 1234,
    "gidNumber": 5678,
    "loginShell": "/bin/bash",
})


_GET_ADGROUP_RECORD = json.dumps({
    "SamAccountName": "Engineers",
    "DistinguishedName": "CN=Engineers,OU=Groups,DC=ad,DC=example,DC=com",
    "Members": [
        "CN=alice,OU=Users,DC=ad,DC=example,DC=com",
        "CN=bob,OU=Users,DC=ad,DC=example,DC=com",
    ],
    "gidNumber": 5678,
})


_GET_LOCALUSER_RECORD = json.dumps({
    "Name": "alice",
    "SID": "S-1-5-21-1234567890-1234567890-1234567890-1001",
    "Enabled": True,
    "FullName": "Alice Doe",
    "Description": "Local user",
})


# ----------------------------------------------------------------------
# Helper: find script call by substring
# ----------------------------------------------------------------------


def _find_calls_containing(runner: _MockCommandRunner, *, needle: str):
    """
        Return every recorded call whose final argv element contains
        ``needle`` as a substring.

        :param runner: The mock runner.
        :param needle: Substring to search for.

        :returns: List of matching call dicts.
    """
    rtnval = []
    for call in runner.calls:
        args = call["args"]
        if len(args) == 0:
            continue
        last = args[-1]
        if needle in last:
            rtnval.append(call)
    return rtnval


# ----------------------------------------------------------------------
# join_realm
# ----------------------------------------------------------------------


def test_join_realm_invokes_add_computer():
    """
        join_realm dispatches a PowerShell scriptblock containing
        ``Add-Computer -DomainName <realm> -Credential $cred ...
        -Force -Restart:$false`` with the password embedded as a
        SecureString.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_WORKGROUP, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Add-Computer",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    spec = WindowsRealmJoinSpec(
        realm="ad.example.com",
        admin_user="DOMAIN\\admin",
        admin_password="topsecret",
        ou_path="OU=Servers,DC=ad,DC=example,DC=com",
        domain_controller="dc1.ad.example.com",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.SUCCESS)

    add_calls = _find_calls_containing(runner, needle="Add-Computer")
    testbase.assert_equal(len(add_calls), 1)
    script = add_calls[0]["args"][-1]
    testbase.assert_true("Add-Computer" in script)
    testbase.assert_true("-DomainName 'ad.example.com'" in script)
    testbase.assert_true("-Credential $cred" in script)
    testbase.assert_true(
        "-OUPath 'OU=Servers,DC=ad,DC=example,DC=com'" in script,
    )
    testbase.assert_true("-Server 'dc1.ad.example.com'" in script)
    testbase.assert_true("-Force" in script)
    testbase.assert_true("-Restart:$false" in script)
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_join_realm_skipped_when_joined():
    """
        join_realm returns SKIPPED when the host already shows joined
        to the requested realm via ``Win32_ComputerSystem``.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    spec = WindowsRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="topsecret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.SKIPPED)
    add_calls = _find_calls_containing(runner, needle="Add-Computer")
    testbase.assert_equal(add_calls, [])
    return


def test_join_realm_dry_run():
    """
        join_realm returns DRY_RUN under ``dry_run=True`` and never
        invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(dry_run=True, elevated=True),
    )
    spec = WindowsRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="topsecret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    # The DRY_RUN result still carries the sanitised argv.
    last_token = result.command[-1]
    testbase.assert_true("topsecret" not in last_token)
    return


# ----------------------------------------------------------------------
# leave_realm
# ----------------------------------------------------------------------


def test_leave_realm_invokes_remove_computer():
    """
        leave_realm dispatches ``Remove-Computer
        -UnjoinDomainCredential $cred -Force -Restart:$false`` when
        the host is currently joined.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Remove-Computer",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="DOMAIN\\admin",
        admin_password="leave-secret",
    )

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.SUCCESS)
    remove_calls = _find_calls_containing(runner, needle="Remove-Computer")
    testbase.assert_equal(len(remove_calls), 1)
    script = remove_calls[0]["args"][-1]
    testbase.assert_true("Remove-Computer" in script)
    testbase.assert_true("-UnjoinDomainCredential $cred" in script)
    testbase.assert_true("-PassThru" in script)
    testbase.assert_true("-Force" in script)
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_leave_realm_skipped_when_not_joined():
    """
        leave_realm returns SKIPPED when ``Win32_ComputerSystem``
        reports the host is not part of a domain.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_WORKGROUP, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="admin",
        admin_password="pw",
    )

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.SKIPPED)
    remove_calls = _find_calls_containing(runner, needle="Remove-Computer")
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# get_realm_status / list_joined_realms
# ----------------------------------------------------------------------


def test_get_realm_status_parses_wmi_output():
    """
        get_realm_status parses ``Win32_ComputerSystem`` JSON and
        reports the joined realm + kind + computer name.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, True)
    testbase.assert_equal(status.current_realm, "ad.example.com")
    testbase.assert_equal(status.realm_kind, WindowsRealmKind.ACTIVE_DIRECTORY)
    testbase.assert_equal(status.computer_name, "WIN-HOST")
    return


def test_get_realm_status_returns_workgroup_when_not_joined():
    """
        get_realm_status reports unjoined when ``PartOfDomain`` is
        False; the parsed Domain field surfaces as the workgroup
        name with ``WindowsRealmKind.WORKGROUP``.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_WORKGROUP, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, False)
    testbase.assert_equal(status.current_realm, "WORKGROUP")
    testbase.assert_equal(status.realm_kind, WindowsRealmKind.WORKGROUP)
    return


def test_list_joined_realms_returns_list():
    """
        list_joined_realms returns a single-element list when joined,
        empty when in a workgroup.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )
    realms = manager.list_joined_realms()

    testbase.assert_equal(len(realms), 1)
    testbase.assert_equal(realms[0].realm, "ad.example.com")
    testbase.assert_equal(realms[0].realm_kind, WindowsRealmKind.ACTIVE_DIRECTORY)
    testbase.assert_equal(realms[0].is_default, True)
    return


# ----------------------------------------------------------------------
# sync_users
# ----------------------------------------------------------------------


def test_sync_users_invokes_klist_purge():
    """
        sync_users dispatches ``klist purge -li 0x3e7`` via
        PowerShell.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    result = manager.sync_users()

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.SUCCESS)
    klist_calls = _find_calls_containing(runner, needle="klist purge")
    testbase.assert_equal(len(klist_calls), 1)
    testbase.assert_true("0x3e7" in klist_calls[0]["args"][-1])
    testbase.assert_equal(klist_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# resolve_user / resolve_group
# ----------------------------------------------------------------------


def test_resolve_user_parses_get_aduser_output():
    """
        resolve_user parses ``Get-ADUser`` JSON output into a
        populated :class:`WindowsUserResolveResult` with the realm
        extracted from the DN.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Get-ADUser",
        result=RunResult(exit_code=0, stdout=_GET_ADUSER_RECORD, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="alice")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.uid, 1234)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.realm, "ad.example.com")
    testbase.assert_equal(
        result.groups, ["Domain Admins", "Engineers"],
    )
    return


def test_resolve_user_falls_back_to_local():
    """
        resolve_user falls back to ``Get-LocalUser`` when
        ``Get-ADUser`` exits non-zero (RSAT not installed).  The
        local result has ``realm=None``.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Get-ADUser",
        result=RunResult(
            exit_code=1,
            stdout="",
            stderr="Get-ADUser is not recognized as the name of a cmdlet",
        ),
    )
    _seed_runner_by_substring(
        runner,
        needle="Get-LocalUser",
        result=RunResult(
            exit_code=0,
            stdout=_GET_LOCALUSER_RECORD,
            stderr="",
        ),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(fallback_to_local=True),
    )
    result = manager.resolve_user(username="alice")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.realm, None)
    return


def test_resolve_user_returns_not_found_when_unknown():
    """
        resolve_user returns ``found=False`` when both ``Get-ADUser``
        and ``Get-LocalUser`` fail.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Get-ADUser",
        result=RunResult(
            exit_code=1,
            stdout="",
            stderr="Cannot find an object with identity",
        ),
    )
    _seed_runner_by_substring(
        runner,
        needle="Get-LocalUser",
        result=RunResult(
            exit_code=1,
            stdout="",
            stderr="User ghost was not found",
        ),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(fallback_to_local=True),
    )
    result = manager.resolve_user(username="ghost")

    testbase.assert_equal(result.found, False)
    return


def test_resolve_group_parses_get_adgroup_output():
    """
        resolve_group parses ``Get-ADGroup`` JSON output into a
        populated :class:`WindowsGroupResolveResult`.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Get-ADGroup",
        result=RunResult(exit_code=0, stdout=_GET_ADGROUP_RECORD, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )
    result = manager.resolve_group(groupname="Engineers")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.realm, "ad.example.com")
    testbase.assert_equal(result.members, ["alice", "bob"])
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_password_redacted_in_recorded_command():
    """
        The recorded ``command`` field after ``join_realm`` MUST NOT
        contain the plaintext password — every embedded
        ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` is
        sanitised to carry ``'<redacted>'`` instead.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_WORKGROUP, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Add-Computer",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    spec = WindowsRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="topsecret-password-value",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_true(result.command is not None)
    last_token = result.command[-1]
    testbase.assert_true("topsecret-password-value" not in last_token)
    testbase.assert_true("<redacted>" in last_token)
    # The runner DID receive the plaintext password on argv.
    add_calls = _find_calls_containing(runner, needle="Add-Computer")
    testbase.assert_equal(len(add_calls), 1)
    runner_script = add_calls[0]["args"][-1]
    testbase.assert_true("topsecret-password-value" in runner_script)
    return


def test_password_redacted_for_leave_realm():
    """
        leave_realm sanitises the embedded password in the recorded
        argv as well.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Remove-Computer",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="admin",
        admin_password="leave-pw",
    )

    last_token = result.command[-1]
    testbase.assert_true("leave-pw" not in last_token)
    testbase.assert_true("<redacted>" in last_token)
    return


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated``.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Remove-Computer",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="klist purge",
        result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    manager.leave_realm(admin_user="admin", admin_password="pw")
    manager.sync_users()

    write_calls = []
    for call in runner.calls:
        last = call["args"][-1] if len(call["args"]) > 0 else ""
        if "Win32_ComputerSystem" in last:
            continue
        if "Remove-Computer" in last or "klist purge" in last:
            write_calls.append(call)

    testbase.assert_true(len(write_calls) >= 2)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never elevate, even when ``elevated=True`` is set
        in the config.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_DOMAIN_JOINED, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Get-ADUser",
        result=RunResult(exit_code=0, stdout=_GET_ADUSER_RECORD, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Get-ADGroup",
        result=RunResult(exit_code=0, stdout=_GET_ADGROUP_RECORD, stderr=""),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    manager.list_joined_realms()
    manager.resolve_user(username="alice")
    manager.resolve_group(groupname="Engineers")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``Add-Computer`` exit produces ``status=FAILED``
        with the captured ``stderr`` and ``exit_code`` on the result.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(exit_code=0, stdout=_WMI_WORKGROUP, stderr=""),
    )
    _seed_runner_by_substring(
        runner,
        needle="Add-Computer",
        result=RunResult(
            exit_code=1,
            stdout="",
            stderr="Add-Computer: Invalid credential supplied\n",
        ),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(elevated=True),
    )
    spec = WindowsRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="bad",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, WindowsIdentityActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true(
        result.stderr is not None and "Invalid credential" in result.stderr,
    )
    return


def test_get_realm_status_raises_on_query_failure():
    """
        get_realm_status raises :class:`WindowsIdentityQueryError`
        when the WMI query exits non-zero.
    """
    runner = _MockCommandRunner()
    _seed_runner_by_substring(
        runner,
        needle="Win32_ComputerSystem",
        result=RunResult(
            exit_code=1,
            stdout="",
            stderr="Get-WmiObject: Access denied\n",
        ),
    )

    manager = WindowsIdentityManager(
        runner=runner,
        config=WindowsIdentityManagerConfig(),
    )

    raised = False
    try:
        manager.get_realm_status()
    except WindowsIdentityQueryError:
        raised = True

    testbase.assert_true(raised)
    return
