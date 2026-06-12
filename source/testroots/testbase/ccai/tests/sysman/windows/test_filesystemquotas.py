"""
    Tests for :class:`WindowsFilesystemQuotasManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``fsutil`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.filesystemquotas import (
    WindowsFilesystemQuotasManager,
    WindowsFilesystemQuotasManagerConfig,
    WindowsQuotaActionStatus,
    WindowsQuotaInfo,
    WindowsQuotaPrincipalKind,
    WindowsQuotaQueryError,
    WindowsQuotaStatus,
    _parse_fsutil_quota_query,
    _quota_token_to_bytes,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple.
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


# ----------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------


_QUERY_BODY_ENABLED_ENFORCING = """\
*  Volume Name = \\\\?\\Volume{abc}\\
*  File System Type Status: NTFS
*  Logging Status:          Enabled
*  Enforce Limit Status:    Enabled
*  Default Quota Threshold: 0xffffffffffffffff
*  Default Quota Limit:     0xffffffffffffffff

  Quota User Name:          BUILTIN\\Administrators (Alias)
  Quota User SID:            S-1-5-32-544
  Change time:              Friday, July 14, 2023 11:01:42 AM
  Quota Used:               1024
  Quota Threshold:          0xffffffffffffffff
  Quota Limit:              0xffffffffffffffff

  Quota User Name:          DOMAIN\\alice
  Quota User SID:            S-1-5-21-1234
  Change time:              Friday, July 14, 2023 11:02:00 AM
  Quota Used:               1048576
  Quota Threshold:          524288000
  Quota Limit:              1073741824
"""


_QUERY_BODY_DISABLED = """\
*  Volume Name = \\\\?\\Volume{abc}\\
*  File System Type Status: NTFS
*  Logging Status:          Disabled
*  Enforce Limit Status:    Disabled
*  Default Quota Threshold: 0xffffffffffffffff
*  Default Quota Limit:     0xffffffffffffffff
"""


def _seed_query(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
    body: str,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``fsutil quota query <mountpoint>`` to return the
        supplied body.
    """
    key = ("fsutil", "quota", "query", mountpoint)
    runner.results[key] = RunResult(
        exit_code=exit_code,
        stdout=body if exit_code == 0 else "",
        stderr=stderr,
    )
    return


# ----------------------------------------------------------------------
# enable_quotas
# ----------------------------------------------------------------------


def test_enable_quotas_invokes_fsutil_track_and_enforce():
    """
        ``enable_quotas`` must invoke
        ``fsutil quota track`` then ``fsutil quota enforce``.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_DISABLED)

    config = WindowsFilesystemQuotasManagerConfig(elevated=True)
    manager = WindowsFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="C:")

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.SUCCESS)

    mutation_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["fsutil", "quota"]
        and call["args"][2] in ("track", "enforce")
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    testbase.assert_equal(
        mutation_calls[0]["args"],
        ["fsutil", "quota", "track", "C:"],
    )
    testbase.assert_equal(
        mutation_calls[1]["args"],
        ["fsutil", "quota", "enforce", "C:"],
    )
    return


def test_enable_quotas_skipped_when_enabled():
    """
        ``enable_quotas`` must return SKIPPED when the volume is
        already enforcing.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="C:")

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.SKIPPED)

    mutation_calls = [
        call for call in runner.calls
        if call["args"][:3] in (
            ["fsutil", "quota", "track"],
            ["fsutil", "quota", "enforce"],
        )
    ]
    testbase.assert_equal(len(mutation_calls), 0)
    return


def test_enable_quotas_dry_run():
    """
        ``enable_quotas`` must return DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    config = WindowsFilesystemQuotasManagerConfig(dry_run=True)
    manager = WindowsFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="C:")

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command,
        ["fsutil", "quota", "track", "C:"],
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# disable_quotas
# ----------------------------------------------------------------------


def test_disable_quotas_invokes_fsutil_disable():
    """
        ``disable_quotas`` must invoke ``fsutil quota disable``.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    result = manager.disable_quotas(mountpoint="C:")

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.SUCCESS)

    disable_calls = [
        call for call in runner.calls
        if call["args"][:3] == ["fsutil", "quota", "disable"]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    testbase.assert_equal(
        disable_calls[0]["args"],
        ["fsutil", "quota", "disable", "C:"],
    )
    return


# ----------------------------------------------------------------------
# set_user_quota
# ----------------------------------------------------------------------


def test_set_user_quota_invokes_fsutil_modify():
    """
        ``set_user_quota`` must invoke
        ``fsutil quota modify <volume> <hard> <soft> <user>``.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_DISABLED)

    config = WindowsFilesystemQuotasManagerConfig(elevated=True)
    manager = WindowsFilesystemQuotasManager(runner=runner, config=config)

    result = manager.set_user_quota(
        mountpoint="C:",
        user="DOMAIN\\bob",
        soft_bytes=524288000,
        hard_bytes=1073741824,
    )

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.SUCCESS)

    modify_calls = [
        call for call in runner.calls
        if call["args"][:3] == ["fsutil", "quota", "modify"]
    ]
    testbase.assert_equal(len(modify_calls), 1)
    testbase.assert_equal(
        modify_calls[0]["args"],
        ["fsutil", "quota", "modify", "C:", "1073741824", "524288000", "DOMAIN\\bob"],
    )
    return


def test_set_user_quota_skipped_when_match():
    """
        ``set_user_quota`` must return SKIPPED when the current
        record already matches.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    result = manager.set_user_quota(
        mountpoint="C:",
        user="DOMAIN\\alice",
        soft_bytes=524288000,
        hard_bytes=1073741824,
    )

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.SKIPPED)

    modify_calls = [
        call for call in runner.calls
        if call["args"][:3] == ["fsutil", "quota", "modify"]
    ]
    testbase.assert_equal(len(modify_calls), 0)
    return


# ----------------------------------------------------------------------
# set_group_quota — NTFS gap
# ----------------------------------------------------------------------


def test_set_group_quota_returns_failed_with_detail():
    """
        ``set_group_quota`` must return FAILED with
        ``detail="ntfs_group_quota_unsupported"`` and must NOT
        invoke any runner command.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemQuotasManager(runner=runner)

    result = manager.set_group_quota(
        mountpoint="C:",
        group="DOMAIN\\Engineers",
        soft_bytes=1024,
        hard_bytes=2048,
    )

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.FAILED)
    testbase.assert_equal(result.detail, "ntfs_group_quota_unsupported")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_user_quota / get_group_quota
# ----------------------------------------------------------------------


def test_get_user_quota_parses_fsutil_output():
    """
        ``get_user_quota`` must parse ``fsutil quota query``
        output for the matching user.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    info = manager.get_user_quota(mountpoint="C:", user="DOMAIN\\alice")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.principal_kind, WindowsQuotaPrincipalKind.USER)
    testbase.assert_equal(info.used_bytes, 1048576)
    testbase.assert_equal(info.soft_bytes, 524288000)
    testbase.assert_equal(info.hard_bytes, 1073741824)
    return


def test_get_user_quota_unknown_returns_none():
    """
        ``get_user_quota`` must return ``None`` for an unknown
        user.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    info = manager.get_user_quota(mountpoint="C:", user="DOMAIN\\ghost")

    testbase.assert_equal(info, None)
    return


def test_get_group_quota_raises_query_error():
    """
        ``get_group_quota`` must raise
        :class:`WindowsQuotaQueryError` carrying
        ``detail="ntfs_group_quota_unsupported"``.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemQuotasManager(runner=runner)

    raised = False
    raised_detail = None
    try:
        manager.get_group_quota(
            mountpoint="C:",
            group="DOMAIN\\Engineers",
        )
    except WindowsQuotaQueryError as ex:
        raised = True
        raised_detail = ex.detail

    testbase.assert_equal(raised, True)
    testbase.assert_equal(raised_detail, "ntfs_group_quota_unsupported")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_get_group_quota_returns_failed_with_detail():
    """
        Alias-style test asserted from the read side: the
        ``detail`` token surfaces the same NTFS gap that
        ``set_group_quota`` reports.
    """
    runner = _MockCommandRunner()
    manager = WindowsFilesystemQuotasManager(runner=runner)

    raised_detail = None
    try:
        manager.get_group_quota(
            mountpoint="C:",
            group="DOMAIN\\Engineers",
        )
    except WindowsQuotaQueryError as ex:
        raised_detail = ex.detail

    testbase.assert_equal(raised_detail, "ntfs_group_quota_unsupported")
    return


# ----------------------------------------------------------------------
# list_quotas
# ----------------------------------------------------------------------


def test_list_quotas_parses_fsutil_output():
    """
        ``list_quotas`` must parse every per-user block.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    entries = manager.list_quotas(mountpoint="C:")

    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(
        all(entry.principal_kind is WindowsQuotaPrincipalKind.USER for entry in entries),
        True,
    )
    return


# ----------------------------------------------------------------------
# get_quota_status
# ----------------------------------------------------------------------


def test_get_quota_status_parses_fsutil_output():
    """
        ``get_quota_status`` must parse the ``Logging Status``
        and ``Enforce Limit Status`` header lines.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    manager = WindowsFilesystemQuotasManager(runner=runner)
    status = manager.get_quota_status(mountpoint="C:")

    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.enforcing, True)
    testbase.assert_equal(status.user_quotas_active, True)
    testbase.assert_equal(status.group_quotas_active, False)
    testbase.assert_equal(status.filesystem_type, "ntfs")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Mutation commands must dispatch with ``elevated=True``
        when ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_DISABLED)

    config = WindowsFilesystemQuotasManagerConfig(elevated=True)
    manager = WindowsFilesystemQuotasManager(runner=runner, config=config)
    manager.enable_quotas(mountpoint="C:")

    mutation_calls = [
        call for call in runner.calls
        if call["args"][:3] in (
            ["fsutil", "quota", "track"],
            ["fsutil", "quota", "enforce"],
        )
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    for call in mutation_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs must dispatch with ``elevated=False``
        regardless of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_ENABLED_ENFORCING)

    config = WindowsFilesystemQuotasManagerConfig(elevated=True)
    manager = WindowsFilesystemQuotasManager(runner=runner, config=config)

    manager.get_user_quota(mountpoint="C:", user="DOMAIN\\alice")
    manager.list_quotas(mountpoint="C:")
    manager.get_quota_status(mountpoint="C:")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``fsutil quota track`` must return ``FAILED``
        with the runner's stderr captured.
    """
    runner = _MockCommandRunner()
    _seed_query(runner, mountpoint="C:", body=_QUERY_BODY_DISABLED)
    runner.results[("fsutil", "quota", "track", "C:")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="The volume does not support quotas.",
    )

    manager = WindowsFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="C:")

    testbase.assert_equal(result.status, WindowsQuotaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "does not support quotas" in result.stderr,
        True,
    )
    return


# ----------------------------------------------------------------------
# Pure-parser tests
# ----------------------------------------------------------------------


def test_quota_token_to_bytes_handles_sentinel():
    """
        The hex sentinel ``0xffffffffffffffff`` must parse to
        ``None``.
    """
    rtnval = _quota_token_to_bytes(token="0xffffffffffffffff")
    testbase.assert_equal(rtnval, None)
    return


def test_quota_token_to_bytes_handles_decimal():
    """
        Decimal byte tokens must parse to integers.
    """
    rtnval = _quota_token_to_bytes(token="1048576")
    testbase.assert_equal(rtnval, 1048576)
    return
