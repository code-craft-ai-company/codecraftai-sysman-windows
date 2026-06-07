"""
    Tests for :class:`WindowsSshServerConfigurationManager`.

    Tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  All file I/O (PowerShell ``Get-Content`` / ``Set-Content``)
    and service interaction (``Restart-Service``) is exercised entirely
    through the runner.
"""

import base64
import hashlib

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.sshserverconfiguration import (
    WindowsSshActionStatus,
    WindowsSshMatchBlock,
    WindowsSshServerConfigurationManager,
    WindowsSshServerConfigurationManagerConfig,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


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


def _read_argv(path: str) -> list:
    """
        Build the Get-Content -Raw argv for a path.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-Content -Path '{}' -Raw -ErrorAction Stop"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _write_argv(path: str) -> list:
    """
        Build the Set-Content argv for a path.
    """
    script = (
        "$content = [Console]::In.ReadToEnd(); "
        "Set-Content -Path '{}' -Value $content -NoNewline -Encoding utf8"
    ).format(path)
    rtnval = _build_powershell_argv(script=script)
    return rtnval


def _build_test_key(*, label: str = "demo@host") -> tuple:
    """
        Synthesise a deterministic OpenSSH ``authorized_keys`` line.
    """
    raw_body = ("test-key-body-" + label).encode("ascii")
    while len(raw_body) % 3 != 0:
        raw_body = raw_body + b"x"
    encoded = base64.b64encode(raw_body).decode("ascii")
    line = "ssh-ed25519 {} {}".format(encoded, label)

    digest_bytes = hashlib.sha256(base64.b64decode(encoded)).digest()
    fingerprint = "SHA256:" + base64.b64encode(
        digest_bytes,
    ).decode("ascii").rstrip("=")
    return (line, fingerprint)


_SSHD_CONFIG_PATH = r"C:\ProgramData\ssh\sshd_config"
_AUTHORIZED_KEYS_PATH = r"C:\Users\alice\.ssh\authorized_keys"
_ADMIN_AUTHORIZED_KEYS_PATH = (
    r"C:\ProgramData\ssh\administrators_authorized_keys"
)


# ----------------------------------------------------------------------
# Read verbs
# ----------------------------------------------------------------------


def test_get_sshd_setting_parses_value():
    """
        get_sshd_setting returns the parsed value from sshd_config.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout=(
            "# header\n"
            "Port 22\n"
            "PermitRootLogin no\n"
            "Match Group administrators\n"
            "    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\n"
        ),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    setting = manager.get_sshd_setting(keyword="PermitRootLogin")

    testbase.assert_equal(setting is not None, True)
    testbase.assert_equal(setting.keyword, "PermitRootLogin")
    testbase.assert_equal(setting.value, "no")
    testbase.assert_equal(setting.source_file, _SSHD_CONFIG_PATH)
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_list_sshd_settings_parses_top_level():
    """
        list_sshd_settings returns top-level keywords only.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout=(
            "Port 22\n"
            "PermitRootLogin no\n"
            "Match Group administrators\n"
            "    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\n"
        ),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    settings = manager.list_sshd_settings()

    testbase.assert_equal(settings["Port"], "22")
    testbase.assert_equal(settings["PermitRootLogin"], "no")
    testbase.assert_equal(len(settings), 2)
    return


def test_list_match_blocks_parses_blocks():
    """
        list_match_blocks parses the stock administrators block.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout=(
            "Port 22\n"
            "Match Group administrators\n"
            "    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\n"
        ),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    blocks = manager.list_match_blocks()

    testbase.assert_equal(len(blocks), 1)
    testbase.assert_equal(blocks[0].criteria, "Group administrators")
    testbase.assert_equal(
        blocks[0].settings["AuthorizedKeysFile"],
        "__PROGRAMDATA__/ssh/administrators_authorized_keys",
    )
    return


def test_list_authorized_keys_parses_user_keys():
    """
        list_authorized_keys parses entries with options + comment.
    """
    runner = _MockCommandRunner()
    line_a, fp_a = _build_test_key(label="alice@laptop")
    line_b, fp_b = _build_test_key(label="alice@desktop")
    options_line = "no-pty,from=\"10.0.0.0/8\" {}".format(line_b)
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=(
            "# main key\n"
            "{}\n"
            "{}\n"
        ).format(line_a, options_line),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    keys = manager.list_authorized_keys(user="alice")

    testbase.assert_equal(len(keys), 2)
    testbase.assert_equal(keys[0].key_type, "ssh-ed25519")
    testbase.assert_equal(keys[0].comment, "alice@laptop")
    testbase.assert_equal(keys[0].fingerprint, fp_a)
    testbase.assert_equal(keys[1].fingerprint, fp_b)
    testbase.assert_equal(
        keys[1].options, 'no-pty,from="10.0.0.0/8"',
    )
    return


def test_list_authorized_keys_uses_admin_path_when_is_admin_true():
    """
        list_authorized_keys reads the admin path when ``is_admin`` is
        True.
    """
    runner = _MockCommandRunner()
    line, fp = _build_test_key(label="admin@host")
    runner.results[tuple(_read_argv(_ADMIN_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=line + "\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    keys = manager.list_authorized_keys(user="admin", is_admin=True)

    testbase.assert_equal(len(keys), 1)
    testbase.assert_equal(keys[0].fingerprint, fp)
    return


# ----------------------------------------------------------------------
# Write verbs — set_sshd_setting
# ----------------------------------------------------------------------


def test_set_sshd_setting_writes_when_absent():
    """
        set_sshd_setting writes via Set-Content when the keyword is
        missing.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_sshd_setting(
        keyword="PermitRootLogin", value="no",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        "PermitRootLogin no" in write_calls[0]["stdin"], True,
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_equal(result.keyword, "PermitRootLogin")
    return


def test_set_sshd_setting_skipped_when_already_set():
    """
        set_sshd_setting returns SKIPPED when the value already
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\nPermitRootLogin no\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_sshd_setting(
        keyword="PermitRootLogin", value="no",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_sshd_setting_replaces_existing_value():
    """
        set_sshd_setting replaces an existing different value.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\nPermitRootLogin yes\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_sshd_setting(
        keyword="PermitRootLogin", value="no",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    rendered = write_calls[0]["stdin"]
    testbase.assert_equal("PermitRootLogin yes" in rendered, False)
    testbase.assert_equal("PermitRootLogin no" in rendered, True)
    return


def test_dry_run_short_circuits_set_sshd_setting():
    """
        Dry-run short-circuits without invoking writes.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(
            dry_run=True, elevated=True,
        ),
    )
    result = manager.set_sshd_setting(
        keyword="PermitRootLogin", value="no",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.DRY_RUN)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    testbase.assert_equal(result.command, _write_argv(_SSHD_CONFIG_PATH))
    return


# ----------------------------------------------------------------------
# remove_sshd_setting
# ----------------------------------------------------------------------


def test_remove_sshd_setting_skipped_when_absent():
    """
        remove_sshd_setting returns SKIPPED when the keyword is not
        present.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    result = manager.remove_sshd_setting(keyword="PermitRootLogin")

    testbase.assert_equal(result.status, WindowsSshActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_remove_sshd_setting_rewrites_file():
    """
        remove_sshd_setting writes the file with the keyword removed.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\nPermitRootLogin yes\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.remove_sshd_setting(keyword="PermitRootLogin")

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    rendered = write_calls[0]["stdin"]
    testbase.assert_equal("PermitRootLogin" in rendered, False)
    testbase.assert_equal("Port 22" in rendered, True)
    return


# ----------------------------------------------------------------------
# Match blocks
# ----------------------------------------------------------------------


def test_add_match_block_appends_to_config():
    """
        add_match_block writes a new Match section to the file.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_match_block(
        criteria="User git",
        settings={"PasswordAuthentication": "no"},
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    rendered = write_calls[0]["stdin"]
    testbase.assert_equal("Match User git" in rendered, True)
    testbase.assert_equal(
        "PasswordAuthentication no" in rendered, True,
    )
    return


def test_remove_match_block_skipped_when_absent():
    """
        remove_match_block returns SKIPPED when no matching block
        exists.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.remove_match_block(criteria="User git")

    testbase.assert_equal(result.status, WindowsSshActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_remove_match_block_rewrites_file():
    """
        remove_match_block rewrites the file with the matched block
        removed.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout=(
            "Port 22\n"
            "Match User git\n"
            "    PasswordAuthentication no\n"
        ),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.remove_match_block(criteria="User git")

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    rendered = write_calls[0]["stdin"]
    testbase.assert_equal("Match" in rendered, False)
    return


# ----------------------------------------------------------------------
# Operational verbs
# ----------------------------------------------------------------------


def test_reload_sshd_invokes_restart_service():
    """
        reload_sshd invokes ``Restart-Service -Name 'sshd'``.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.reload_sshd()

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(last_call["args"][0], "powershell.exe")
    testbase.assert_equal(
        "Restart-Service -Name 'sshd'" in last_call["args"][-1], True,
    )
    testbase.assert_equal(last_call["elevated"], True)
    return


def test_test_sshd_config_invokes_sshd_dash_t():
    """
        test_sshd_config invokes ``sshd.exe -t``.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.test_sshd_config()

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["sshd.exe", "-t"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Authorized-key verbs
# ----------------------------------------------------------------------


def test_add_authorized_key_writes_to_per_user_path():
    """
        add_authorized_key writes the per-user authorized_keys.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout="",
    )

    line, _ = _build_test_key(label="alice@laptop")

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_authorized_key(user="alice", key=line)

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(_AUTHORIZED_KEYS_PATH in write_calls[0]["args"][-1], True)
    testbase.assert_equal(line in write_calls[0]["stdin"], True)
    return


def test_add_authorized_key_writes_to_admin_path_when_is_admin_true():
    """
        add_authorized_key writes
        ``%ProgramData%\\ssh\\administrators_authorized_keys`` when
        is_admin is True (Windows admin-vs-non-admin distinction).
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_ADMIN_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout="",
    )

    line, _ = _build_test_key(label="admin@host")

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_authorized_key(
        user="admin", key=line, is_admin=True,
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        _ADMIN_AUTHORIZED_KEYS_PATH in write_calls[0]["args"][-1], True,
    )
    testbase.assert_equal(
        _AUTHORIZED_KEYS_PATH in write_calls[0]["args"][-1], False,
    )
    return


def test_add_authorized_key_skipped_when_already_present():
    """
        add_authorized_key returns SKIPPED when the same fingerprint
        is already present.
    """
    runner = _MockCommandRunner(default_result=_ok())
    line, _ = _build_test_key(label="alice@laptop")
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=line + "\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_authorized_key(user="alice", key=line)

    testbase.assert_equal(result.status, WindowsSshActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_add_authorized_key_failed_on_parse_error():
    """
        add_authorized_key returns FAILED when the key fails to parse.
    """
    runner = _MockCommandRunner()

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_authorized_key(
        user="alice", key="not a real key",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.FAILED)
    testbase.assert_equal(result.message, "key line failed to parse")
    return


def test_remove_authorized_key_finds_by_fingerprint():
    """
        remove_authorized_key removes the line whose fingerprint
        matches.
    """
    runner = _MockCommandRunner(default_result=_ok())
    line_a, fp_a = _build_test_key(label="alice@laptop")
    line_b, fp_b = _build_test_key(label="alice@desktop")
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=line_a + "\n" + line_b + "\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.remove_authorized_key(
        user="alice", fingerprint=fp_a,
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    written = write_calls[0]["stdin"]
    testbase.assert_equal(line_a in written, False)
    testbase.assert_equal(line_b in written, True)
    return


def test_remove_authorized_key_skipped_when_fingerprint_absent():
    """
        remove_authorized_key returns SKIPPED when no matching line.
    """
    runner = _MockCommandRunner(default_result=_ok())
    line, _ = _build_test_key(label="alice@laptop")
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=line + "\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.remove_authorized_key(
        user="alice", fingerprint="SHA256:nope",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation, error surfaces
# ----------------------------------------------------------------------


def test_writes_pass_elevated_true():
    """
        Every mutating verb passes ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    manager.set_sshd_setting(keyword="PermitRootLogin", value="no")
    manager.reload_sshd()
    manager.test_sshd_config()

    write_calls = [
        c for c in runner.calls
        if "Get-Content" not in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls) > 0, True)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\nMatch User git\n    PasswordAuthentication no\n",
    )
    line, _ = _build_test_key(label="alice@laptop")
    runner.results[tuple(_read_argv(_AUTHORIZED_KEYS_PATH))] = _ok(
        stdout=line + "\n",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    manager.list_sshd_settings()
    manager.get_sshd_setting(keyword="Port")
    manager.list_match_blocks()
    manager.list_authorized_keys(user="alice")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_returns_failed():
    """
        A failed Set-Content write maps to FAILED with stderr /
        exit_code.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout="Port 22\n",
    )
    runner.results[tuple(_write_argv(_SSHD_CONFIG_PATH))] = _fail(
        stderr="Set-Content : Access to the path is denied.",
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_sshd_setting(
        keyword="PermitRootLogin", value="no",
    )

    testbase.assert_equal(result.status, WindowsSshActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "Set-Content" in result.stderr, True,
    )
    return


# ----------------------------------------------------------------------
# Parser corner cases
# ----------------------------------------------------------------------


def test_parser_handles_match_block_followed_by_eof():
    """
        list_match_blocks captures the last block when it runs to EOF.
    """
    runner = _MockCommandRunner()
    runner.results[tuple(_read_argv(_SSHD_CONFIG_PATH))] = _ok(
        stdout=(
            "Port 22\n"
            "Match User git\n"
            "    AllowTcpForwarding yes\n"
        ),
    )

    manager = WindowsSshServerConfigurationManager(
        runner=runner,
        config=WindowsSshServerConfigurationManagerConfig(),
    )
    blocks = manager.list_match_blocks()

    testbase.assert_equal(len(blocks), 1)
    testbase.assert_equal(
        isinstance(blocks[0], WindowsSshMatchBlock), True,
    )
    testbase.assert_equal(blocks[0].criteria, "User git")
    testbase.assert_equal(blocks[0].settings["AllowTcpForwarding"], "yes")
    return
