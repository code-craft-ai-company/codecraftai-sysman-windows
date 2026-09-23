"""
    Tests for :class:`WindowsEncryptionManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call (argv + elevation flag + stdin payload) and returns pre-seeded
    :class:`RunResult` values keyed by argv tuple.  No live PowerShell
    is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.encryptionmanagement import (
    WindowsEncryptionActionStatus,
    WindowsEncryptionManager,
    WindowsEncryptionManagerConfig,
    WindowsEncryptionSpec,
    _DESTRUCTIVE_GUARD_DETAIL,
    _is_record_encrypted,
    _parse_get_bitlockervolume,
    _sanitise_script_passwords,
)


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


# ----------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------


_VOLUME = "C:"


def _status_probe_argv(*, volume: str) -> tuple:
    """
        Return the canonical argv for the ``Get-BitLockerVolume``
        idempotency probe.
    """
    script = (
        "Get-BitLockerVolume -MountPoint '{}' "
        "| ConvertTo-Json -Compress -Depth 5"
    ).format(volume)
    rtnval = (
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
    )
    return rtnval


def _seed_status_record(
    *,
    runner: _MockCommandRunner,
    volume: str,
    record: dict,
) -> None:
    """
        Seed the runner so the ``Get-BitLockerVolume`` probe returns
        a JSON object built from ``record``.
    """
    key = _status_probe_argv(volume=volume)
    payload = json.dumps(record)
    runner.results[key] = RunResult(exit_code=0, stdout=payload, stderr="")
    return


def _seed_status_absent(
    *,
    runner: _MockCommandRunner,
    volume: str,
) -> None:
    """
        Seed the runner so the ``Get-BitLockerVolume`` probe returns
        a non-zero exit (the way the cmdlet signals absence).
    """
    key = _status_probe_argv(volume=volume)
    runner.results[key] = RunResult(
        exit_code=1, stdout="", stderr="No such volume",
    )
    return


# ----------------------------------------------------------------------
# enable_encryption
# ----------------------------------------------------------------------


def test_enable_encryption_invokes_enable_bitlocker():
    """
        enable_encryption dispatches a PowerShell scriptblock that
        calls ``Enable-BitLocker -MountPoint 'C:'
        -EncryptionMethod XtsAes256 -PasswordProtector ...``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "Off",
            "VolumeStatus": "FullyDecrypted",
            "EncryptionPercentage": 0,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "None",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    spec = WindowsEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_VOLUME, spec=spec)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Enable-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    script = write_calls[0]["args"][-1]
    testbase.assert_true("Enable-BitLocker -MountPoint 'C:'" in script)
    testbase.assert_true("-EncryptionMethod XtsAes256" in script)
    testbase.assert_true("-PasswordProtector" in script)
    testbase.assert_true("-RecoveryPasswordProtector" in script)
    testbase.assert_true("-SkipHardwareTest" in script)
    return


def test_enable_encryption_skipped_when_already_encrypted():
    """
        enable_encryption returns SKIPPED when the volume's
        ProtectionStatus is already On.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [
                {"KeyProtectorType": "Tpm", "KeyProtectorId": "{abc}"},
                {"KeyProtectorType": "RecoveryPassword", "KeyProtectorId": "{def}"},
            ],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    spec = WindowsEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_VOLUME, spec=spec)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if "Enable-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_enable_encryption_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(dry_run=True),
    )
    spec = WindowsEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_VOLUME, spec=spec)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true("Enable-BitLocker" in result.command[-1])
    return


# ----------------------------------------------------------------------
# disable_encryption
# ----------------------------------------------------------------------


def test_disable_encryption_requires_allow_destructive():
    """
        disable_encryption returns FAILED with the destructive-guard
        detail when ``allow_destructive`` is False.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [
                {"KeyProtectorType": "RecoveryPassword", "KeyProtectorId": "{def}"},
            ],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(allow_destructive=False),
    )
    result = manager.disable_encryption(volume=_VOLUME)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.detail, _DESTRUCTIVE_GUARD_DETAIL)
    disable_calls = [
        c for c in runner.calls
        if "Disable-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(disable_calls, [])
    return


def test_disable_encryption_invokes_disable_bitlocker_when_allowed():
    """
        disable_encryption dispatches ``Disable-BitLocker -MountPoint
        '<vol>'`` when ``allow_destructive=True`` and the volume is
        currently encrypted.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(allow_destructive=True),
    )
    result = manager.disable_encryption(volume=_VOLUME)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SUCCESS)
    disable_calls = [
        c for c in runner.calls
        if "Disable-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    return


# ----------------------------------------------------------------------
# get_encryption_status
# ----------------------------------------------------------------------


def test_get_encryption_status_parses_output():
    """
        get_encryption_status parses the cmdlet's JSON record and
        emits a populated WindowsEncryptionStatus.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [
                {"KeyProtectorType": "Tpm", "KeyProtectorId": "{abc}"},
                {"KeyProtectorType": "RecoveryPassword", "KeyProtectorId": "{def}"},
            ],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    status = manager.get_encryption_status(volume=_VOLUME)

    testbase.assert_equal(status.is_encrypted, True)
    testbase.assert_equal(status.is_unlocked, True)
    testbase.assert_equal(status.cipher, "XtsAes256")
    testbase.assert_equal(status.protector_types, ["Tpm", "RecoveryPassword"])
    return


# ----------------------------------------------------------------------
# list_encrypted_volumes
# ----------------------------------------------------------------------


def test_list_encrypted_volumes_filters_correctly():
    """
        list_encrypted_volumes returns one row per volume whose
        ``ProtectionStatus=On`` (or ``VolumeStatus=FullyEncrypted``).
        Plain volumes are filtered out.
    """
    runner = _MockCommandRunner()
    list_argv = (
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Get-BitLockerVolume | ConvertTo-Json -Compress -Depth 5",
    )
    runner.results[list_argv] = RunResult(
        exit_code=0,
        stdout=json.dumps([
            {
                "MountPoint": "C:",
                "ProtectionStatus": "On",
                "VolumeStatus": "FullyEncrypted",
                "EncryptionPercentage": 100,
                "LockStatus": "Unlocked",
                "EncryptionMethod": "XtsAes256",
                "KeyProtector": [],
            },
            {
                "MountPoint": "D:",
                "ProtectionStatus": "Off",
                "VolumeStatus": "FullyDecrypted",
                "EncryptionPercentage": 0,
                "LockStatus": "Unlocked",
                "EncryptionMethod": "None",
                "KeyProtector": [],
            },
            {
                "MountPoint": "E:",
                "ProtectionStatus": "On",
                "VolumeStatus": "FullyEncrypted",
                "EncryptionPercentage": 100,
                "LockStatus": "Locked",
                "EncryptionMethod": "XtsAes128",
                "KeyProtector": [],
            },
        ]),
        stderr="",
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    volumes = manager.list_encrypted_volumes()

    testbase.assert_equal(len(volumes), 2)
    testbase.assert_equal(volumes[0].volume, "C:")
    testbase.assert_equal(volumes[0].is_unlocked, True)
    testbase.assert_equal(volumes[1].volume, "E:")
    testbase.assert_equal(volumes[1].is_unlocked, False)
    return


# ----------------------------------------------------------------------
# rotate_key
# ----------------------------------------------------------------------


def test_rotate_key_invokes_add_then_remove_bitlocker_key_protector():
    """
        rotate_key dispatches a scriptblock that adds the new
        password protector before removing the old one.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    result = manager.rotate_key(volume=_VOLUME, new_passphrase="new-secret")

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Add-BitLockerKeyProtector" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    script = write_calls[0]["args"][-1]
    add_index = script.find("Add-BitLockerKeyProtector")
    remove_index = script.find("Remove-BitLockerKeyProtector")
    testbase.assert_true(add_index >= 0)
    testbase.assert_true(remove_index >= 0)
    testbase.assert_true(add_index < remove_index)
    return


# ----------------------------------------------------------------------
# backup_recovery_key
# ----------------------------------------------------------------------


def test_backup_recovery_key_writes_to_path():
    """
        backup_recovery_key dispatches a scriptblock that pulls the
        RecoveryPassword protector and writes it to ``path`` via
        ``Set-Content``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    result = manager.backup_recovery_key(
        volume=_VOLUME, path="C:\\BitLockerKeys\\C-recovery.txt",
    )

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if "Set-Content" in c["args"][-1]
    ]
    testbase.assert_equal(len(write_calls), 1)
    script = write_calls[0]["args"][-1]
    testbase.assert_true(
        "Where-Object KeyProtectorType -eq 'RecoveryPassword'" in script,
    )
    testbase.assert_true(
        "Set-Content -Path 'C:\\BitLockerKeys\\C-recovery.txt'" in script,
    )
    return


# ----------------------------------------------------------------------
# unlock_volume
# ----------------------------------------------------------------------


def test_unlock_volume_passes_passphrase_via_secure_string():
    """
        unlock_volume dispatches a scriptblock that wraps the
        password via ``ConvertTo-SecureString`` and calls
        ``Unlock-BitLocker -MountPoint '<vol>' -Password $pw``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Locked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_VOLUME, passphrase="hunter2")

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SUCCESS)
    unlock_calls = [
        c for c in runner.calls
        if "Unlock-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(len(unlock_calls), 1)
    script = unlock_calls[0]["args"][-1]
    testbase.assert_true("Unlock-BitLocker -MountPoint 'C:' -Password $pw" in script)
    return


def test_unlock_volume_skipped_when_unlocked():
    """
        unlock_volume returns SKIPPED when ``Get-BitLockerVolume``
        reports ``LockStatus=Unlocked``.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_VOLUME, passphrase="hunter2")

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.SKIPPED)
    unlock_calls = [
        c for c in runner.calls
        if "Unlock-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(unlock_calls, [])
    return


# ----------------------------------------------------------------------
# Password / argv hygiene
# ----------------------------------------------------------------------


def test_passphrase_redacted_in_recorded_command():
    """
        Plaintext passwords inside the dispatched scriptblock are
        replaced with ``<redacted>`` in the recorded ``command``
        field on the result.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "Off",
            "VolumeStatus": "FullyDecrypted",
            "EncryptionPercentage": 0,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "None",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    spec = WindowsEncryptionSpec(passphrase="super-secret-string")
    result = manager.enable_encryption(volume=_VOLUME, spec=spec)

    recorded_script = result.command[-1]
    testbase.assert_true("super-secret-string" not in recorded_script)
    testbase.assert_true("<redacted>" in recorded_script)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` to the runner when
        the config requests elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "Off",
            "VolumeStatus": "FullyDecrypted",
            "EncryptionPercentage": 0,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "None",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(elevated=True),
    )
    spec = WindowsEncryptionSpec(passphrase="hunter2")
    manager.enable_encryption(volume=_VOLUME, spec=spec)

    enable_calls = [
        c for c in runner.calls
        if "Enable-BitLocker" in c["args"][-1]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_equal(enable_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (and idempotency probes) never pass
        ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "On",
            "VolumeStatus": "FullyEncrypted",
            "EncryptionPercentage": 100,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "XtsAes256",
            "KeyProtector": [],
        },
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(elevated=True),
    )
    manager.get_encryption_status(volume=_VOLUME)

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED results carry the runner's exit_code and stderr.
    """
    runner = _MockCommandRunner()
    _seed_status_record(
        runner=runner,
        volume=_VOLUME,
        record={
            "MountPoint": "C:",
            "ProtectionStatus": "Off",
            "VolumeStatus": "FullyDecrypted",
            "EncryptionPercentage": 0,
            "LockStatus": "Unlocked",
            "EncryptionMethod": "None",
            "KeyProtector": [],
        },
    )
    runner.default_result = RunResult(
        exit_code=2, stdout="", stderr="The volume cannot be encrypted",
    )

    manager = WindowsEncryptionManager(
        runner=runner,
        config=WindowsEncryptionManagerConfig(),
    )
    spec = WindowsEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_VOLUME, spec=spec)

    testbase.assert_equal(result.status, WindowsEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "The volume cannot be encrypted")
    return


# ----------------------------------------------------------------------
# Module-level parser helpers
# ----------------------------------------------------------------------


def test_parse_get_bitlockervolume_handles_single_object():
    """
        _parse_get_bitlockervolume folds a single-object payload
        (the ConvertTo-Json shape for one match) into a single-
        element list.
    """
    payload = json.dumps(
        {"MountPoint": "C:", "ProtectionStatus": "On"},
    )
    rows = _parse_get_bitlockervolume(text=payload)
    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(rows[0]["MountPoint"], "C:")
    return


def test_is_record_encrypted_recognises_string_and_int_protection():
    """
        _is_record_encrypted recognises both string ("On") and int
        (1) values for ProtectionStatus.
    """
    testbase.assert_equal(
        _is_record_encrypted(record={"ProtectionStatus": "On"}), True,
    )
    testbase.assert_equal(
        _is_record_encrypted(record={"ProtectionStatus": 1}), True,
    )
    testbase.assert_equal(
        _is_record_encrypted(record={"ProtectionStatus": "Off"}), False,
    )
    return


def test_sanitise_script_passwords_redacts_literal():
    """
        _sanitise_script_passwords replaces the ``ConvertTo-SecureString``
        literal password with the redaction sentinel.
    """
    script = (
        "$pw = ConvertTo-SecureString -AsPlainText -Force 'hunter2'; "
        "Enable-BitLocker -MountPoint 'C:' -Password $pw"
    )
    sanitised = _sanitise_script_passwords(script=script)
    testbase.assert_true("hunter2" not in sanitised)
    testbase.assert_true("<redacted>" in sanitised)
    return
