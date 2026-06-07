"""
    Tests for :class:`WindowsUserGroupManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live PowerShell process is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.usergroupmanagement import (
    WindowsGroupInfo,
    WindowsGroupSpec,
    WindowsPrincipalKind,
    WindowsUserChanges,
    WindowsUserGroupActionResult,
    WindowsUserGroupActionStatus,
    WindowsUserGroupManager,
    WindowsUserGroupManagerConfig,
    WindowsUserGroupQueryError,
    WindowsUserInfo,
    WindowsUserSpec,
    _parse_get_localgroup,
    _parse_get_localgroupmember,
    _parse_get_localuser,
    _sanitise_script_passwords,
    _strip_domain_prefix,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _build_powershell_argv(script: str) -> list:
    """
        Build the canonical PowerShell argv used by the manager.
    """
    rtnval = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    return rtnval


def _user_exists_argv(name: str) -> list:
    """
        Build the argv the manager produces for the ``_user_exists``
        existence probe of ``name``.
    """
    script = (
        "Get-LocalUser -Name '{}' "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _group_exists_argv(name: str) -> list:
    """
        Build the argv the manager produces for the ``_group_exists``
        existence probe of ``name``.
    """
    script = (
        "Get-LocalGroup -Name '{}' "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _get_user_record_argv(name: str) -> list:
    """
        Build the argv the manager produces for ``_get_user_record``.
    """
    script = (
        "Get-LocalUser -Name '{}' "
        "| Select-Object Name, SID, Enabled, FullName, Description, "
        "LastLogon, PasswordNeverExpires, AccountExpires "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(name)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _list_users_argv() -> list:
    """
        Build the argv the manager produces for :meth:`list_users`.
    """
    script = (
        "Get-LocalUser "
        "| Select-Object Name, SID, Enabled, FullName, Description, "
        "LastLogon, PasswordNeverExpires, AccountExpires "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _list_groups_argv() -> list:
    """
        Build the argv the manager produces for :meth:`list_groups`.
    """
    script = (
        "Get-LocalGroup "
        "| Select-Object Name, SID, Description "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _get_localgroupmember_argv(group: str) -> list:
    """
        Build the argv the manager produces for
        ``_enumerate_group_members``.
    """
    script = (
        "Get-LocalGroupMember -Group '{}' "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(group)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _enumerate_groups_argv() -> list:
    """
        Build the argv the manager produces for the
        ``_enumerate_user_groups`` group-list probe.
    """
    script = (
        "Get-LocalGroup "
        "| Select-Object Name "
        "| ConvertTo-Json -Compress -Depth 5"
    )
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _user_record_payload(
    *,
    name: str,
    enabled: bool = True,
    full_name: Optional[str] = "Test User",
    description: Optional[str] = None,
    sid: str = "S-1-5-21-0-0-0-1000",
    password_never_expires: bool = False,
) -> str:
    """
        Build a representative ``Get-LocalUser | ConvertTo-Json``
        payload.
    """
    record = {
        "Name": name,
        "SID": {"Value": sid},
        "Enabled": enabled,
        "FullName": full_name,
        "Description": description,
        "LastLogon": None,
        "PasswordNeverExpires": password_never_expires,
        "AccountExpires": None,
    }
    rtnval = json.dumps(record)
    return rtnval


def _group_record_payload(
    *,
    name: str,
    sid: str = "S-1-5-32-545",
    description: Optional[str] = None,
) -> str:
    """
        Build a representative ``Get-LocalGroup | ConvertTo-Json``
        payload.
    """
    record = {
        "Name": name,
        "SID": {"Value": sid},
        "Description": description,
    }
    rtnval = json.dumps(record)
    return rtnval


# ----------------------------------------------------------------------
# create_user
# ----------------------------------------------------------------------


def test_create_user_constructs_new_localuser_script():
    """
        create_user dispatches ``New-LocalUser`` with a
        SecureString-wrapped password.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    spec = WindowsUserSpec(
        name="alice", password="hunter2", full_name="Alice A",
    )
    result = manager.create_user(spec=spec)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    testbase.assert_equal(result.kind, WindowsPrincipalKind.USER)
    testbase.assert_equal(result.name, "alice")

    write_calls = [
        c for c in runner.calls
        if "New-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    invoked_script = write_calls[0]["args"][-1]
    testbase.assert_true("New-LocalUser -Name 'alice'" in invoked_script)
    testbase.assert_true("-Password $pw" in invoked_script)
    testbase.assert_true(
        "ConvertTo-SecureString -AsPlainText -Force 'hunter2'"
        in invoked_script,
    )
    testbase.assert_true("-FullName 'Alice A'" in invoked_script)
    return


def test_create_user_password_redacted_in_recorded_command():
    """
        The plaintext password embedded in the create_user
        scriptblock is redacted in the recorded ``command`` field of
        the result, but is preserved in the argv actually sent to the
        runner.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    spec = WindowsUserSpec(name="alice", password="hunter2")
    result = manager.create_user(spec=spec)

    # Recorded argv must redact the password.
    recorded_script = result.command[-1]
    testbase.assert_true("hunter2" not in recorded_script)
    testbase.assert_true("<redacted>" in recorded_script)

    # Runner argv must carry the plaintext.
    write_calls = [
        c for c in runner.calls
        if "New-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    runner_script = write_calls[0]["args"][-1]
    testbase.assert_true("hunter2" in runner_script)
    return


def test_create_user_skipped_when_present():
    """
        create_user returns SKIPPED when the user already exists.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice"),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    spec = WindowsUserSpec(name="alice", password="hunter2")
    result = manager.create_user(spec=spec)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "New-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_create_user_dry_run_short_circuits():
    """
        Dry-run short-circuits create_user without invoking the runner
        and the recorded command still has the password redacted.
    """
    runner = _MockCommandRunner()
    manager = WindowsUserGroupManager(
        runner=runner,
        config=WindowsUserGroupManagerConfig(dry_run=True),
    )
    spec = WindowsUserSpec(name="alice", password="hunter2")
    result = manager.create_user(spec=spec)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    recorded_script = result.command[-1]
    testbase.assert_true("hunter2" not in recorded_script)
    testbase.assert_true("<redacted>" in recorded_script)
    return


# ----------------------------------------------------------------------
# delete_user
# ----------------------------------------------------------------------


def test_delete_user_invokes_remove_localuser():
    """
        delete_user on an existing user dispatches ``Remove-LocalUser``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice"),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.delete_user(name="alice")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Remove-LocalUser -Name 'alice'",
    )
    write_calls = [
        c for c in runner.calls
        if "Remove-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_delete_user_skipped_when_absent():
    """
        delete_user returns SKIPPED when the user does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_user_exists_argv(name="ghost"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.delete_user(name="ghost")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Remove-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# modify_user
# ----------------------------------------------------------------------


def test_modify_user_emits_only_changed_attributes():
    """
        modify_user only emits writes for attributes that differ from
        the current observed state.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(
            name="alice", full_name="Alice A", description="old",
        ),
        stderr="",
    )
    runner.results[tuple(_enumerate_groups_argv())] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )

    # full_name unchanged; description changed
    changes = WindowsUserChanges(full_name="Alice A", description="new desc")
    result = manager.modify_user(name="alice", changes=changes)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if "Set-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(set_calls), 1)
    invoked_script = set_calls[0]["args"][-1]
    testbase.assert_true("-Description 'new desc'" in invoked_script)
    testbase.assert_true("-FullName" not in invoked_script)
    return


def test_modify_user_skipped_when_noop():
    """
        modify_user returns SKIPPED when nothing in ``changes``
        differs from the observed state.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(
            name="alice", full_name="Alice A",
        ),
        stderr="",
    )
    runner.results[tuple(_enumerate_groups_argv())] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    changes = WindowsUserChanges(full_name="Alice A")
    result = manager.modify_user(name="alice", changes=changes)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if "Set-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_modify_user_failed_when_user_missing():
    """
        modify_user returns FAILED with a documenting message when the
        user does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_user_record_argv(name="ghost"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    changes = WindowsUserChanges(full_name="Ghost")
    result = manager.modify_user(name="ghost", changes=changes)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.message, "user does not exist")
    return


def test_modify_user_password_redacted_in_recorded_command():
    """
        modify_user with a password change records the argv with the
        plaintext password redacted.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice"),
        stderr="",
    )
    runner.results[tuple(_enumerate_groups_argv())] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    changes = WindowsUserChanges(password="newpw")
    result = manager.modify_user(name="alice", changes=changes)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    recorded_script = result.command[-1]
    testbase.assert_true("newpw" not in recorded_script)
    testbase.assert_true("<redacted>" in recorded_script)
    return


# ----------------------------------------------------------------------
# set_password
# ----------------------------------------------------------------------


def test_set_password_dispatches_set_localuser():
    """
        set_password dispatches a ``Set-LocalUser -Password`` script
        that wraps the plaintext password via ``ConvertTo-SecureString``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.set_password(user="alice", password="newpw")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    runner_script = runner.calls[0]["args"][-1]
    testbase.assert_true(
        "Set-LocalUser -Name 'alice' -Password $pw" in runner_script,
    )
    testbase.assert_true(
        "ConvertTo-SecureString -AsPlainText -Force 'newpw'"
        in runner_script,
    )
    # Recorded command has the password redacted.
    recorded_script = result.command[-1]
    testbase.assert_true("newpw" not in recorded_script)
    testbase.assert_true("<redacted>" in recorded_script)
    return


# ----------------------------------------------------------------------
# lock_account / unlock_account
# ----------------------------------------------------------------------


def test_lock_account_skipped_when_already_disabled():
    """
        lock_account returns SKIPPED when the user's ``Enabled`` flag
        is already False.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice", enabled=False),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.lock_account(user="alice")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    disable_calls = [
        c for c in runner.calls
        if "Disable-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(disable_calls, [])
    return


def test_lock_account_invokes_disable_localuser():
    """
        lock_account dispatches ``Disable-LocalUser`` when the user is
        currently enabled.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice", enabled=True),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.lock_account(user="alice")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    disable_calls = [
        c for c in runner.calls
        if "Disable-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    expected = _build_powershell_argv(
        script="Disable-LocalUser -Name 'alice'",
    )
    testbase.assert_equal(disable_calls[0]["args"], expected)
    return


def test_unlock_account_skipped_when_already_enabled():
    """
        unlock_account returns SKIPPED when ``Enabled`` is True.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_get_user_record_argv(name="alice"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="alice", enabled=True),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.unlock_account(user="alice")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if "Enable-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(enable_calls, [])
    return


# ----------------------------------------------------------------------
# create_group / delete_group
# ----------------------------------------------------------------------


def test_create_group_constructs_new_localgroup_script():
    """
        create_group dispatches ``New-LocalGroup`` with the optional
        description.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_group_exists_argv(name="qa"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    spec = WindowsGroupSpec(name="qa", description="Quality")
    result = manager.create_group(spec=spec)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    testbase.assert_equal(result.kind, WindowsPrincipalKind.GROUP)
    expected = _build_powershell_argv(
        script="New-LocalGroup -Name 'qa' -Description 'Quality'",
    )
    write_calls = [
        c for c in runner.calls
        if "New-LocalGroup" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], expected)
    return


def test_create_group_skipped_when_present():
    """
        create_group returns SKIPPED when the group already exists.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_group_exists_argv(name="qa"))] = RunResult(
        exit_code=0,
        stdout=_group_record_payload(name="qa"),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    spec = WindowsGroupSpec(name="qa")
    result = manager.create_group(spec=spec)
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "New-LocalGroup" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_delete_group_skipped_when_absent():
    """
        delete_group returns SKIPPED when the group does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_group_exists_argv(name="ghost"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.delete_group(name="ghost")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Remove-LocalGroup" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# add_to_group / remove_from_group
# ----------------------------------------------------------------------


def test_add_to_group_skipped_when_already_member():
    """
        add_to_group returns SKIPPED when the user is already a
        member.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_group_exists_argv(name="qa"))] = RunResult(
        exit_code=0,
        stdout=_group_record_payload(name="qa"),
        stderr="",
    )
    runner.results[tuple(_get_localgroupmember_argv(group="qa"))] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"Name": "MACHINE\\alice"}]),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.add_to_group(user="alice", group="qa")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    add_calls = [
        c for c in runner.calls
        if "Add-LocalGroupMember" in c["args"][-1]
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_add_to_group_invokes_add_localgroupmember():
    """
        add_to_group dispatches ``Add-LocalGroupMember`` when the
        target is not yet a member.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_group_exists_argv(name="qa"))] = RunResult(
        exit_code=0,
        stdout=_group_record_payload(name="qa"),
        stderr="",
    )
    runner.results[tuple(_get_localgroupmember_argv(group="qa"))] = RunResult(
        exit_code=0,
        stdout=json.dumps([]),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.add_to_group(user="alice", group="qa")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SUCCESS)
    expected = _build_powershell_argv(
        script="Add-LocalGroupMember -Group 'qa' -Member 'alice'",
    )
    add_calls = [
        c for c in runner.calls
        if "Add-LocalGroupMember" in c["args"][-1]
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(add_calls[0]["args"], expected)
    return


def test_remove_from_group_skipped_when_not_member():
    """
        remove_from_group returns SKIPPED when the user is not a
        member of the group.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_group_exists_argv(name="qa"))] = RunResult(
        exit_code=0,
        stdout=_group_record_payload(name="qa"),
        stderr="",
    )
    runner.results[tuple(_get_localgroupmember_argv(group="qa"))] = RunResult(
        exit_code=0,
        stdout=json.dumps([]),
        stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.remove_from_group(user="alice", group="qa")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.SKIPPED)
    return


def test_add_to_group_failed_when_group_missing():
    """
        add_to_group returns FAILED when the group does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_group_exists_argv(name="ghost"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.add_to_group(user="alice", group="ghost")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.message, "group does not exist")
    return


# ----------------------------------------------------------------------
# Read verbs / parsers
# ----------------------------------------------------------------------


def test_list_users_parses_array_form():
    """
        list_users parses the JSON array emitted by ``Get-LocalUser |
        ConvertTo-Json``.
    """
    records = [
        {
            "Name": "alice",
            "SID": {"Value": "S-1-5-21-0-0-0-1000"},
            "Enabled": True,
            "FullName": "Alice A",
            "Description": None,
            "LastLogon": None,
            "PasswordNeverExpires": False,
            "AccountExpires": None,
        },
        {
            "Name": "bob",
            "SID": {"Value": "S-1-5-21-0-0-0-1001"},
            "Enabled": False,
            "FullName": "Bob B",
            "Description": "test user",
            "LastLogon": "2026-01-01",
            "PasswordNeverExpires": True,
            "AccountExpires": None,
        },
    ]
    runner = _MockCommandRunner()
    runner.results[tuple(_list_users_argv())] = RunResult(
        exit_code=0, stdout=json.dumps(records), stderr="",
    )
    runner.results[tuple(_enumerate_groups_argv())] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    listed = manager.list_users()
    testbase.assert_equal(len(listed), 2)
    first = listed[0]
    testbase.assert_equal(first.name, "alice")
    testbase.assert_equal(first.sid, "S-1-5-21-0-0-0-1000")
    testbase.assert_equal(first.enabled, True)
    testbase.assert_equal(first.full_name, "Alice A")
    testbase.assert_equal(first.password_never_expires, False)

    second = listed[1]
    testbase.assert_equal(second.name, "bob")
    testbase.assert_equal(second.enabled, False)
    testbase.assert_equal(second.password_never_expires, True)
    return


def test_list_groups_parses_array_form():
    """
        list_groups parses the JSON array emitted by ``Get-LocalGroup |
        ConvertTo-Json``.
    """
    records = [
        {"Name": "Administrators", "SID": "S-1-5-32-544", "Description": "Admins"},
        {"Name": "Users", "SID": {"Value": "S-1-5-32-545"}, "Description": None},
    ]
    runner = _MockCommandRunner()
    runner.results[tuple(_list_groups_argv())] = RunResult(
        exit_code=0, stdout=json.dumps(records), stderr="",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    listed = manager.list_groups()
    testbase.assert_equal(len(listed), 2)
    testbase.assert_equal(listed[0].name, "Administrators")
    testbase.assert_equal(listed[0].sid, "S-1-5-32-544")
    testbase.assert_equal(listed[0].description, "Admins")
    testbase.assert_equal(listed[1].name, "Users")
    testbase.assert_equal(listed[1].sid, "S-1-5-32-545")
    testbase.assert_equal(listed[1].description, None)
    return


def test_get_localgroupmember_parser_strips_domain_prefix():
    """
        Module-level helpers parse Get-LocalGroupMember rows and the
        domain-prefix stripper drops any ``DOMAIN\\`` prefix from
        compound names.
    """
    raw = json.dumps(
        [{"Name": "MACHINE\\alice"}, {"Name": "MACHINE\\bob"}],
    )
    parsed = _parse_get_localgroupmember(text=raw)
    testbase.assert_equal(parsed, ["MACHINE\\alice", "MACHINE\\bob"])
    testbase.assert_equal(_strip_domain_prefix(raw_name="MACHINE\\alice"),
                          "alice")
    testbase.assert_equal(_strip_domain_prefix(raw_name="alice"), "alice")
    return


def test_parse_get_localuser_handles_bare_object():
    """
        ``Get-LocalUser | ConvertTo-Json`` may emit a single bare
        object instead of a one-element array.  The parser folds that
        into a one-element list.
    """
    record = {
        "Name": "Solo",
        "SID": "S-1-5-21-0-0-0-1234",
        "Enabled": True,
    }
    parsed = _parse_get_localuser(text=json.dumps(record))
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0]["Name"], "Solo")
    return


def test_parse_get_localgroup_handles_array_form():
    """
        ``_parse_get_localgroup`` returns the array verbatim.
    """
    records = [
        {"Name": "Administrators", "SID": "S-1-5-32-544"},
        {"Name": "Users", "SID": "S-1-5-32-545"},
    ]
    parsed = _parse_get_localgroup(text=json.dumps(records))
    testbase.assert_equal(len(parsed), 2)
    testbase.assert_equal(parsed[0]["Name"], "Administrators")
    testbase.assert_equal(parsed[1]["Name"], "Users")
    return


# ----------------------------------------------------------------------
# Password redaction helper
# ----------------------------------------------------------------------


def test_sanitise_script_passwords_replaces_every_match():
    """
        The script-level password sanitiser replaces every embedded
        ``ConvertTo-SecureString -AsPlainText -Force '<pw>'`` with the
        same wrapper carrying ``<redacted>`` instead.
    """
    script = (
        "$pw = ConvertTo-SecureString -AsPlainText -Force 'first'; "
        "$pw2 = ConvertTo-SecureString -AsPlainText -Force 'second'"
    )
    cleaned = _sanitise_script_passwords(script=script)
    testbase.assert_true("first" not in cleaned)
    testbase.assert_true("second" not in cleaned)
    testbase.assert_true(cleaned.count("<redacted>") == 2)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_when_configured():
    """
        Writes pass ``elevated=True`` to the runner when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner,
        config=WindowsUserGroupManagerConfig(elevated=True),
    )
    spec = WindowsUserSpec(name="alice", password="hunter2")
    manager.create_user(spec=spec)
    write_calls = [
        c for c in runner.calls
        if "New-LocalUser" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Idempotency-probe and direct read commands never elevate, even
        when the config is configured to elevate writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner,
        config=WindowsUserGroupManagerConfig(elevated=True),
    )
    spec = WindowsUserSpec(name="alice", password="hunter2")
    manager.create_user(spec=spec)

    probe_calls = [
        c for c in runner.calls
        if "Get-LocalUser" in c["args"][-1]
    ]
    testbase.assert_true(len(probe_calls) >= 1)
    for call in probe_calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfaces / argv shape
# ----------------------------------------------------------------------


def test_failed_write_returns_failed_with_stderr():
    """
        A non-zero exit from the write maps to FAILED with the
        captured stderr / exit_code preserved.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_user_exists_argv(name="ghost"))] = RunResult(
        exit_code=0,
        stdout=_user_record_payload(name="ghost"),
        stderr="",
    )
    delete_argv = _build_powershell_argv(
        script="Remove-LocalUser -Name 'ghost'",
    )
    runner.results[tuple(delete_argv)] = RunResult(
        exit_code=1, stdout="", stderr="Access is denied",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    result = manager.delete_user(name="ghost")
    testbase.assert_equal(result.status, WindowsUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Access is denied")
    return


def test_powershell_argv_construction_includes_noprofile():
    """
        Every dispatched command runs PowerShell with ``-NoProfile``
        and ``-NonInteractive`` so $PROFILE / interactive prompts
        cannot mutate cmdlet behaviour.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[tuple(_user_exists_argv(name="alice"))] = RunResult(
        exit_code=1, stdout="", stderr="not found",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    manager.create_user(spec=WindowsUserSpec(name="alice", password="x"))
    for call in runner.calls:
        argv = call["args"]
        testbase.assert_equal(argv[0], "powershell.exe")
        testbase.assert_equal(argv[1], "-NoProfile")
        testbase.assert_equal(argv[2], "-NonInteractive")
        testbase.assert_equal(argv[3], "-Command")
        testbase.assert_equal(len(argv), 5)
    return


def test_list_users_query_error_on_failure():
    """
        list_users raises :class:`WindowsUserGroupQueryError` when
        ``Get-LocalUser`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_list_users_argv())] = RunResult(
        exit_code=1, stdout="", stderr="something broke",
    )
    manager = WindowsUserGroupManager(
        runner=runner, config=WindowsUserGroupManagerConfig(),
    )
    raised = False
    try:
        manager.list_users()
    except WindowsUserGroupQueryError:
        raised = True
    testbase.assert_true(raised)
    return
