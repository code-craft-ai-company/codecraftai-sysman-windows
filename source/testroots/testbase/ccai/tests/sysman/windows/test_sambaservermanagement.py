"""
    Tests for :class:`WindowsSambaServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple OR by PowerShell-script prefix.  No live
    PowerShell is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.sambaservermanagement import (
    WindowsSambaActionStatus,
    WindowsSambaManagerConfig,
    WindowsSambaQueryError,
    WindowsSambaServerManager,
    WindowsSambaShareAcl,
    WindowsSambaShareInfo,
    WindowsSambaShareSpec,
    WindowsSambaUserInfo,
    _build_new_smb_share_script,
    _compute_acl_change_set,
    _parse_shares_json,
    _users_from_access_records,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple OR by PowerShell-script prefix.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)
    script_results: dict = field(default_factory=dict)

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

        if (
            len(args) == 5
            and isinstance(args[0], str)
            and args[0].lower().startswith("powershell")
        ):
            script = args[4]
            for key_prefix, result in self.script_results.items():
                if script.startswith(key_prefix):
                    return result

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


def _seed_share_lookup_by_name(
    runner: _MockCommandRunner,
    *,
    name: str,
    json_text: str,
    exit_code: int = 0,
) -> None:
    """
        Seed ``Get-SmbShare -Name '<name>'``.
    """
    prefix = "Get-SmbShare -Name '{}' ".format(name)
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code, stdout=json_text, stderr="",
    )
    return


def _seed_get_share_all(
    runner: _MockCommandRunner,
    *,
    json_text: str,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``Get-SmbShare | ConvertTo-Json``.
    """
    prefix = "Get-SmbShare | "
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code, stdout=json_text, stderr=stderr,
    )
    return


def _seed_get_share_access_all(
    runner: _MockCommandRunner,
    *,
    json_text: str,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``Get-SmbShareAccess -Name '*'``.
    """
    prefix = "Get-SmbShareAccess -Name '*' "
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code, stdout=json_text, stderr=stderr,
    )
    return


def _seed_get_share_access_named(
    runner: _MockCommandRunner,
    *,
    name: str,
    json_text: str,
    exit_code: int = 0,
) -> None:
    """
        Seed ``Get-SmbShareAccess -Name '<name>'``.
    """
    prefix = "Get-SmbShareAccess -Name '{}' ".format(name)
    runner.script_results[prefix] = RunResult(
        exit_code=exit_code, stdout=json_text, stderr="",
    )
    return


def _make_spec(
    *,
    name: str = "public",
    path: str = "D:\\Shares\\Public",
    description: Optional[str] = None,
    read_only: bool = False,
    valid_users: Optional[list] = None,
    browseable: bool = True,
    guest_ok: bool = False,
) -> WindowsSambaShareSpec:
    spec = WindowsSambaShareSpec(
        name=name,
        path=path,
        description=description,
        read_only=read_only,
        valid_users=valid_users,
        browseable=browseable,
        guest_ok=guest_ok,
    )
    return spec


_SHARE_PUBLIC_JSON = (
    '{"Name":"public","Path":"D:\\\\Shares\\\\Public",'
    '"Description":"Public share","FolderEnumerationMode":'
    '"Unrestricted","CurrentUsers":0}'
)


# ----------------------------------------------------------------------
# add_share
# ----------------------------------------------------------------------


def test_add_share_invokes_new_smbshare():
    """
        add_share dispatches ``New-SmbShare`` when the share is
        not yet present.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)
    spec = _make_spec(
        name="public",
        path="D:\\Shares\\Public",
        valid_users=["DOMAIN\\admin"],
    )

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SUCCESS)
    testbase.assert_equal(result.share_name, "public")

    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][0].lower().startswith("powershell")
            and call["args"][4].startswith("New-SmbShare")
        )
    ]
    testbase.assert_equal(len(new_share_calls), 1)
    script = new_share_calls[0]["args"][4]
    testbase.assert_equal("-Name 'public'" in script, True)
    testbase.assert_equal("-Path 'D:\\Shares\\Public'" in script, True)
    testbase.assert_equal("-FullAccess" in script, True)
    testbase.assert_equal("DOMAIN\\admin" in script, True)
    testbase.assert_equal(new_share_calls[0]["elevated"], True)
    return


def test_add_share_skipped_when_present():
    """
        add_share returns SKIPPED when ``Get-SmbShare -Name`` already
        reports a share with matching name and path.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text=_SHARE_PUBLIC_JSON,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)
    spec = _make_spec(
        name="public", path="D:\\Shares\\Public",
    )

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SKIPPED)
    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("New-SmbShare")
        )
    ]
    testbase.assert_equal(len(new_share_calls), 0)
    return


def test_add_share_dry_run():
    """
        add_share under ``dry_run=True`` returns DRY_RUN and never
        invokes ``New-SmbShare``.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True, dry_run=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command[4].startswith("New-SmbShare"), True,
    )

    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("New-SmbShare")
        )
    ]
    testbase.assert_equal(len(new_share_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_share
# ----------------------------------------------------------------------


def test_remove_share_invokes_remove_smbshare():
    """
        remove_share dispatches ``Remove-SmbShare`` when the share
        is registered.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="public", json_text=_SHARE_PUBLIC_JSON,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SUCCESS)

    remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("Remove-SmbShare")
        )
    ]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(
        "Remove-SmbShare -Name 'public' -Force" in remove_calls[0]["args"][4],
        True,
    )
    return


def test_remove_share_skipped_when_absent():
    """
        remove_share returns SKIPPED when no share with the given
        name is registered.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# list_shares
# ----------------------------------------------------------------------


def test_list_shares_parses_output():
    """
        list_shares parses ``Get-SmbShare`` JSON into
        :class:`WindowsSambaShareInfo` records.
    """
    json_text = (
        '[{"Name":"public","Path":"D:\\\\Shares\\\\Public",'
        '"Description":"Public share","FolderEnumerationMode":'
        '"Unrestricted","CurrentUsers":3},'
        '{"Name":"private","Path":"D:\\\\Shares\\\\Private",'
        '"Description":"","FolderEnumerationMode":"AccessBased",'
        '"CurrentUsers":0}]'
    )
    runner = _MockCommandRunner()
    _seed_get_share_all(runner, json_text=json_text)
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig()
    manager = WindowsSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_shares()
    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.name: entry for entry in rtnval}
    testbase.assert_equal(by_name["public"].browseable, True)
    testbase.assert_equal(by_name["public"].connection_count, 3)
    testbase.assert_equal(by_name["private"].browseable, False)
    return


# ----------------------------------------------------------------------
# get_share
# ----------------------------------------------------------------------


def test_get_share_returns_correct_record():
    """
        get_share returns the matching record and ``None`` for a
        missing share.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text=_SHARE_PUBLIC_JSON,
    )
    _seed_share_lookup_by_name(
        runner, name="absent", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig()
    manager = WindowsSambaServerManager(runner=runner, config=config)

    found = manager.get_share(name="public")
    testbase.assert_equal(found is not None, True)
    testbase.assert_equal(found.name, "public")

    missing = manager.get_share(name="absent")
    testbase.assert_equal(missing, None)
    return


# ----------------------------------------------------------------------
# set_share_permissions
# ----------------------------------------------------------------------


def test_set_share_permissions_dispatches_grant_and_revoke():
    """
        set_share_permissions dispatches a Grant-SmbShareAccess
        per principal in the read / write / full-access lists and
        a Revoke-SmbShareAccess per principal in the denied list.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="public", json_text=_SHARE_PUBLIC_JSON,
    )
    _seed_get_share_access_named(
        runner, name="public", json_text="",
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    acl = WindowsSambaShareAcl(
        read_users=["DOMAIN\\reader"],
        write_users=["DOMAIN\\writer"],
        full_access_users=["DOMAIN\\admin"],
        denied_users=["DOMAIN\\mallory"],
    )

    result = manager.set_share_permissions(name="public", acl=acl)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SUCCESS)

    grant_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("Grant-SmbShareAccess")
        )
    ]
    revoke_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("Revoke-SmbShareAccess")
        )
    ]
    testbase.assert_equal(len(grant_calls), 3)
    testbase.assert_equal(len(revoke_calls), 1)

    rights_seen: list = []
    for call in grant_calls:
        script = call["args"][4]
        if "-AccessRight Full" in script:
            rights_seen.append("Full")
        if "-AccessRight Change" in script:
            rights_seen.append("Change")
        if "-AccessRight Read" in script:
            rights_seen.append("Read")
    testbase.assert_equal(sorted(rights_seen), ["Change", "Full", "Read"])
    return


def test_set_share_permissions_skipped_when_match():
    """
        set_share_permissions returns SKIPPED when the existing
        access map already matches the requested ACL.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text=_SHARE_PUBLIC_JSON,
    )
    _seed_get_share_access_named(
        runner,
        name="public",
        json_text=(
            '[{"Name":"public","AccountName":"DOMAIN\\\\admin",'
            '"AccessRight":"Full","AccessControlType":"Allow"},'
            '{"Name":"public","AccountName":"DOMAIN\\\\reader",'
            '"AccessRight":"Read","AccessControlType":"Allow"}]'
        ),
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    acl = WindowsSambaShareAcl(
        read_users=["DOMAIN\\reader"],
        write_users=[],
        full_access_users=["DOMAIN\\admin"],
        denied_users=[],
    )
    result = manager.set_share_permissions(name="public", acl=acl)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SKIPPED)

    grant_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("Grant-SmbShareAccess")
        )
    ]
    testbase.assert_equal(len(grant_calls), 0)
    return


def test_set_share_permissions_skipped_when_share_absent():
    """
        set_share_permissions returns SKIPPED when no share with
        the given name is registered.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="absent", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    acl = WindowsSambaShareAcl(
        read_users=[],
        write_users=[],
        full_access_users=["DOMAIN\\admin"],
        denied_users=[],
    )
    result = manager.set_share_permissions(name="absent", acl=acl)
    testbase.assert_equal(result.status, WindowsSambaActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# reload_config
# ----------------------------------------------------------------------


def test_reload_config_invokes_no_op_success():
    """
        reload_config returns SUCCESS without invoking the runner.
    """
    runner = _MockCommandRunner()

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    result = manager.reload_config()

    testbase.assert_equal(result.status, WindowsSambaActionStatus.SUCCESS)
    testbase.assert_equal(
        "no-op" in (result.message or ""), True,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_reload_config_dry_run():
    """
        reload_config under dry_run=True returns DRY_RUN.
    """
    runner = _MockCommandRunner()

    config = WindowsSambaManagerConfig(elevated=True, dry_run=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    result = manager.reload_config()

    testbase.assert_equal(result.status, WindowsSambaActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# add_samba_user / remove_samba_user delegation
# ----------------------------------------------------------------------


def test_add_samba_user_returns_failed_with_delegation_message():
    """
        add_samba_user always returns FAILED with the delegation
        message under the default ``delegate_user_mgmt=True``.
    """
    runner = _MockCommandRunner()

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    secret = "topSecretP@ss"
    result = manager.add_samba_user(name="alice", password=secret)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.FAILED)
    testbase.assert_equal(
        result.detail, "add_samba_user delegated to user-group-management",
    )
    testbase.assert_equal(
        "user-group-management" in result.message, True,
    )
    testbase.assert_equal(result.username, "alice")

    testbase.assert_equal(len(runner.calls), 0)

    recorded = " ".join(result.command)
    testbase.assert_equal(secret in recorded, False)
    return


def test_remove_samba_user_returns_failed_with_delegation_message():
    """
        remove_samba_user always returns FAILED with the delegation
        message.
    """
    runner = _MockCommandRunner()

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    result = manager.remove_samba_user(name="alice")

    testbase.assert_equal(result.status, WindowsSambaActionStatus.FAILED)
    testbase.assert_equal(
        result.detail,
        "remove_samba_user delegated to user-group-management",
    )
    testbase.assert_equal(result.username, "alice")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# list_samba_users
# ----------------------------------------------------------------------


def test_list_samba_users_parses_get_smbshareaccess_output():
    """
        list_samba_users parses ``Get-SmbShareAccess`` JSON into
        per-account :class:`WindowsSambaUserInfo` records.
    """
    runner = _MockCommandRunner()
    _seed_get_share_access_all(
        runner,
        json_text=(
            '[{"Name":"public","AccountName":"DOMAIN\\\\admin",'
            '"AccessRight":"Full","AccessControlType":"Allow"},'
            '{"Name":"private","AccountName":"DOMAIN\\\\reader",'
            '"AccessRight":"Read","AccessControlType":"Allow"},'
            '{"Name":"public","AccountName":"DOMAIN\\\\mallory",'
            '"AccessRight":"Full","AccessControlType":"Deny"}]'
        ),
    )

    config = WindowsSambaManagerConfig()
    manager = WindowsSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_samba_users()
    by_name = {entry.username: entry for entry in rtnval}
    testbase.assert_equal(len(rtnval), 3)
    testbase.assert_equal(by_name["DOMAIN\\admin"].enabled, True)
    testbase.assert_equal(by_name["DOMAIN\\reader"].enabled, True)
    testbase.assert_equal(by_name["DOMAIN\\mallory"].enabled, False)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` to the runner when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    spec = _make_spec(name="public", valid_users=["DOMAIN\\admin"])
    manager.add_share(spec=spec)

    new_share_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("New-SmbShare")
        )
    ]
    testbase.assert_equal(len(new_share_calls), 1)
    testbase.assert_equal(new_share_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs and idempotency probes never pass
        ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_get_share_all(runner, json_text="")
    _seed_get_share_access_all(runner, json_text="")
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)

    manager.list_shares()
    manager.get_share(name="public")
    manager.list_samba_users()

    for call in runner.calls:
        if (
            len(call["args"]) == 5
            and call["args"][4].startswith("Get-")
        ):
            testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``New-SmbShare`` produces a FAILED result with
        the runner's stderr, exit code, and command attached.
    """
    runner = _MockCommandRunner()
    _seed_share_lookup_by_name(
        runner, name="public", json_text="", exit_code=1,
    )
    _seed_get_share_access_all(runner, json_text="")
    runner.script_results["New-SmbShare"] = _fail(
        stderr="New-SmbShare: failed", exit_code=2,
    )

    config = WindowsSambaManagerConfig(elevated=True)
    manager = WindowsSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public", valid_users=["DOMAIN\\admin"])

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, WindowsSambaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("New-SmbShare: failed" in result.stderr, True)
    testbase.assert_equal(
        result.command[4].startswith("New-SmbShare"), True,
    )
    return


def test_list_shares_raises_query_error_on_unexpected_failure():
    """
        list_shares raises :class:`WindowsSambaQueryError` when
        the cmdlet exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    _seed_get_share_all(
        runner,
        json_text="",
        exit_code=1,
        stderr="Get-SmbShare: not authorised",
    )
    _seed_get_share_access_all(runner, json_text="")

    config = WindowsSambaManagerConfig()
    manager = WindowsSambaServerManager(runner=runner, config=config)

    raised: Optional[WindowsSambaQueryError] = None
    try:
        manager.list_shares()
    except WindowsSambaQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Renderer / parser unit tests
# ----------------------------------------------------------------------


def test_build_new_smb_share_script_includes_full_access():
    """
        ``_build_new_smb_share_script`` emits ``-FullAccess`` when
        ``valid_users`` is supplied.
    """
    spec = _make_spec(
        name="public",
        path="D:\\Shares\\Public",
        valid_users=["DOMAIN\\admin"],
    )
    rendered = _build_new_smb_share_script(spec=spec)
    testbase.assert_equal("-Name 'public'" in rendered, True)
    testbase.assert_equal("-FullAccess" in rendered, True)
    testbase.assert_equal("-FolderEnumerationMode" in rendered, True)
    return


def test_parse_shares_json_handles_single_record():
    """
        ``_parse_shares_json`` accepts both single-object and list
        JSON shapes.
    """
    parsed = _parse_shares_json(
        text=_SHARE_PUBLIC_JSON, access_map={},
    )
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0].name, "public")
    return


def test_compute_acl_change_set_suppresses_existing_grants():
    """
        ``_compute_acl_change_set`` returns an empty list when the
        existing access map already matches the requested ACL.
    """
    existing = [
        {
            "Name": "public",
            "AccountName": "DOMAIN\\admin",
            "AccessRight": "Full",
            "AccessControlType": "Allow",
        },
    ]
    acl = WindowsSambaShareAcl(
        read_users=[],
        write_users=[],
        full_access_users=["DOMAIN\\admin"],
        denied_users=[],
    )
    rendered = _compute_acl_change_set(existing=existing, acl=acl)
    testbase.assert_equal(rendered, [])
    return


def test_users_from_access_records_collapses_per_principal():
    """
        ``_users_from_access_records`` produces one user record per
        unique account name across the access map.
    """
    records = [
        {
            "Name": "public",
            "AccountName": "DOMAIN\\admin",
            "AccessRight": "Full",
            "AccessControlType": "Allow",
        },
        {
            "Name": "private",
            "AccountName": "DOMAIN\\admin",
            "AccessRight": "Read",
            "AccessControlType": "Allow",
        },
    ]
    rendered = _users_from_access_records(records=records)
    testbase.assert_equal(len(rendered), 1)
    testbase.assert_equal(rendered[0].username, "DOMAIN\\admin")
    testbase.assert_equal(rendered[0].enabled, True)
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_share_info_dataclass_shape():
    """
        :class:`WindowsSambaShareInfo` exposes the contracted
        fields.
    """
    info = WindowsSambaShareInfo(
        name="public",
        path="D:\\Shares\\Public",
        description=None,
        read_only=False,
        valid_users=None,
        read_list=None,
        write_list=None,
        browseable=True,
        guest_ok=False,
        is_active=True,
        connection_count=None,
    )
    testbase.assert_equal(info.is_active, True)
    return


def test_user_info_dataclass_shape():
    """
        :class:`WindowsSambaUserInfo` exposes the contracted
        fields.
    """
    info = WindowsSambaUserInfo(
        username="DOMAIN\\admin",
        enabled=True,
        last_password_change=None,
        description="Full",
    )
    testbase.assert_equal(info.username, "DOMAIN\\admin")
    return
