# Design

## Overview

The security compliance feature is implemented as a single module,
`automojo.sysman.linux.windows.securitycompliance`, exposing a
`SecurityComplianceManager` class.  The manager provides five capabilities: SELinux
status querying and mode enforcement, file permission auditing and remediation, SSH
daemon configuration auditing and hardening, world-writable and SUID/SGID file
discovery, and a pluggable compliance check runner that produces a structured
`ComplianceReport`.

All external I/O is delegated to `IFileOperator` and `ICommandRunner`.  No subprocess or
file system access is performed directly.  The module has no third-party dependencies.

---

## Architecture

```
┌──────────────────────────────────────────┐
│        Maintenance Coordinator           │  (external package)
└──────────────────┬───────────────────────┘
                   │ calls
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│              SecurityComplianceManager                           │
│  check_selinux()            set_selinux_mode()                  │
│  check_file_permissions()   remediate_file_permissions()         │
│  check_ssh_config()         apply_ssh_hardening()               │
│  find_world_writable_files() find_suid_sgid_files()             │
│  run_compliance_checks()                                        │
└───────────────┬───────────────────────────┬──────────────────────┘
                │ file I/O                  │ commands
                ▼                           ▼
┌──────────────────────┐    ┌────────────────────────────┐
│   IFileOperator      │    │   ICommandRunner           │
│   read_text()        │    │   run(cmd, elevated)       │
│   write_text()       │    └────────────────────────────┘
└──────────────────────┘
```

---

## Components

### `SecurityComplianceManager`

**Location:** `source/packages/automojo/sysman/linux/windows/securitycompliance.py`

| Method | Signature | Description |
|---|---|---|
| `check_selinux` | `() → SELinuxStatus` | Query `getenforce` and `sestatus` |
| `set_selinux_mode` | `(mode: SELinuxMode) → ComplianceActionResult` | Run `setenforce` |
| `check_file_permissions` | `(checks: list[FilePermissionCheck]) → list[FilePermissionResult]` | Run `stat` per path |
| `remediate_file_permissions` | `(results: list[FilePermissionResult]) → list[ComplianceActionResult]` | Run `chmod`/`chown` |
| `check_ssh_config` | `(baseline: dict[str, str] \| None = None) → list[SSHConfigFinding]` | Read and audit sshd_config |
| `apply_ssh_hardening` | `(settings: dict[str, str]) → ComplianceActionResult` | Patch and restart sshd |
| `find_world_writable_files` | `(search_paths: list[str]) → list[str]` | Run `find -perm -0002` |
| `find_suid_sgid_files` | `(search_paths: list[str]) → list[str]` | Run `find -perm -4000/-2000` |
| `run_compliance_checks` | `(checks: list[ComplianceCheck]) → ComplianceReport` | Execute check suite |

### `SecurityComplianceConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Skip all mutating operations when `True` |
| `ssh_config_path` | `str` | `"/etc/ssh/sshd_config"` | Path to SSH daemon configuration |

---

## Data Models

### `SELinuxMode`

```python
class SELinuxMode(Enum):
    ENFORCING  = "enforcing"
    PERMISSIVE = "permissive"
    DISABLED   = "disabled"
```

### `SELinuxStatus`

```python
@dataclass
class SELinuxStatus:
    mode: SELinuxMode
    policy: str | None = None   # e.g. "targeted"; None if sestatus unavailable
```

### `FilePermissionCheck`

```python
@dataclass
class FilePermissionCheck:
    path: str
    expected_mode: str      # octal string e.g. "644"  (without leading zero)
    expected_owner: str     # e.g. "root"
    expected_group: str     # e.g. "root"
```

### `FilePermissionResult`

```python
@dataclass
class FilePermissionResult:
    path: str
    compliant: bool
    expected_mode: str
    expected_owner: str
    expected_group: str
    actual_mode: str | None = None
    actual_owner: str | None = None
    actual_group: str | None = None
    error: str | None = None
```

### `SSHConfigFinding`

```python
@dataclass
class SSHConfigFinding:
    setting: str            # e.g. "PermitRootLogin"
    expected: str           # e.g. "no"
    actual: str | None      # None when setting is absent or commented out
    compliant: bool
    severity: ComplianceSeverity
```

### `ComplianceSeverity`

```python
class ComplianceSeverity(Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
```

### `ComplianceCheck`

```python
@dataclass
class ComplianceCheck:
    check_id: str
    description: str
    check_type: str          # "selinux_mode" | "file_permission" | "ssh_setting"
                             # | "no_world_writable" | "no_unexpected_suid"
    severity: ComplianceSeverity
    # type-specific fields (only one group is set per instance):
    expected_value: str | None = None      # selinux_mode, ssh_setting
    file_check: FilePermissionCheck | None = None   # file_permission
    setting_key: str | None = None         # ssh_setting: key to check
    search_path: str | None = None         # no_world_writable, no_unexpected_suid
    allowed_paths: list[str] | None = None # no_unexpected_suid: known-good SUID list
```

### `ComplianceCheckResult`

```python
@dataclass
class ComplianceCheckResult:
    check_id: str
    description: str
    severity: ComplianceSeverity
    check_type: str
    compliant: bool
    finding: str | None = None   # human-readable description of the non-compliance
    error: str | None = None     # set when an exception prevented evaluation
```

### `ComplianceReport`

```python
@dataclass
class ComplianceReport:
    results: list[ComplianceCheckResult]
    total: int
    compliant: int
    non_compliant: int
    errored: int
```

### `ComplianceActionResult`

```python
@dataclass
class ComplianceActionResult:
    success: bool
    target: str | None = None   # path or setting name the action targeted
    dry_run: bool = False
    error: str | None = None
```

### `SecurityQueryError`

```python
class SecurityQueryError(Exception):
    """Raised when a read-only query command fails."""
```

---

## Default SSH Hardening Baseline

When `check_ssh_config` is called without a `baseline`, the following default mapping is
used.  Severity is embedded in a parallel `_SSH_BASELINE_SEVERITY` mapping used only by
`check_ssh_config`:

| Setting | Expected Value | Severity |
|---|---|---|
| `PermitRootLogin` | `no` | `CRITICAL` |
| `PasswordAuthentication` | `no` | `HIGH` |
| `PermitEmptyPasswords` | `no` | `CRITICAL` |
| `X11Forwarding` | `no` | `MEDIUM` |
| `MaxAuthTries` | `4` | `MEDIUM` |
| `Protocol` | `2` | `HIGH` |
| `IgnoreRhosts` | `yes` | `MEDIUM` |
| `HostbasedAuthentication` | `no` | `MEDIUM` |
| `UsePAM` | `yes` | `LOW` |

When a custom baseline is supplied, all findings are given `severity=ComplianceSeverity.MEDIUM`
unless the caller also supplies a severity mapping via the optional
`baseline_severity: dict[str, ComplianceSeverity]` parameter.

---

## `check_selinux()` Logic

```
1. stdout, _ = command_runner.run(["getenforce"], elevated=True)
2. mode = {
       "enforcing":  SELinuxMode.ENFORCING,
       "permissive": SELinuxMode.PERMISSIVE,
       "disabled":   SELinuxMode.DISABLED,
   }[stdout.strip().lower()]   → KeyError raises SecurityQueryError
3. Try: stdout2, _ = command_runner.run(["sestatus"], elevated=True)
   Parse lines for "Policy from config file:" or "Loaded policy name:"; extract policy.
4. Return SELinuxStatus(mode=mode, policy=policy)
```

Non-zero exit from either command → raise `SecurityQueryError`.

---

## `check_file_permissions()` Logic

For each `FilePermissionCheck`:

```
stat_cmd = ["stat", "-c", "%a %U %G", check.path]
stdout, stderr = command_runner.run(stat_cmd, elevated=True)
if "No such file" in stderr or exit_code != 0:
    → FilePermissionResult(compliant=False, error=...)
else:
    actual_mode, actual_owner, actual_group = stdout.strip().split()
    compliant = (actual_mode == check.expected_mode and
                 actual_owner == check.expected_owner and
                 actual_group == check.expected_group)
    → FilePermissionResult(compliant=compliant, actual_*=actual_*, ...)
```

---

## `check_ssh_config()` Parsing

```
content = file_operator.read_text(config.ssh_config_path, elevated=True)
active = {}      # key → value for non-commented lines
for line in content.splitlines():
    stripped = line.strip()
    if stripped.startswith("#") or not stripped:
        continue
    parts = stripped.split(None, 1)
    if len(parts) == 2:
        active[parts[0]] = parts[1]
for setting, expected in baseline.items():
    actual = active.get(setting)          # None if absent
    compliant = (actual == expected)
    → SSHConfigFinding(setting, expected, actual, compliant, severity)
```

---

## `apply_ssh_hardening()` Line-Replacement Logic

```
lines = content.splitlines()
result_lines = list(lines)
for key, value in settings.items():
    new_line = f"{key} {value}"
    replaced = False
    for i, line in enumerate(result_lines):
        # match active or commented-out occurrence
        if re.match(rf"^\s*#?\s*{re.escape(key)}\s+", line):
            result_lines[i] = new_line
            replaced = True
            break        # replace first match only
    if not replaced:
        result_lines.append(new_line)
updated_content = "\n".join(result_lines) + "\n"
```

Only the **first** matching line (active or commented-out) is replaced; subsequent
duplicate lines are left unchanged.  The file is then written back with elevated
privileges, and `systemctl restart sshd` is run.

---

## `run_compliance_checks()` Dispatch

```python
for check in checks:
    try:
        if check.check_type == "selinux_mode":
            status = self.check_selinux()
            compliant = status.mode.value == check.expected_value
            finding = None if compliant else f"mode is {status.mode.value}"
        elif check.check_type == "file_permission":
            result = self.check_file_permissions([check.file_check])[0]
            compliant = result.compliant
            finding = None if compliant else _permission_finding(result)
        elif check.check_type == "ssh_setting":
            findings = self.check_ssh_config({check.setting_key: check.expected_value})
            f = findings[0]
            compliant = f.compliant
            finding = None if compliant else f"actual={f.actual!r}"
        elif check.check_type == "no_world_writable":
            paths = self.find_world_writable_files([check.search_path])
            compliant = len(paths) == 0
            finding = None if compliant else f"{len(paths)} world-writable file(s): {paths[:5]}"
        elif check.check_type == "no_unexpected_suid":
            paths = self.find_suid_sgid_files([check.search_path])
            unexpected = [p for p in paths if p not in (check.allowed_paths or [])]
            compliant = len(unexpected) == 0
            finding = None if compliant else f"{len(unexpected)} unexpected SUID/SGID: {unexpected[:5]}"
        results.append(ComplianceCheckResult(..., compliant=compliant, finding=finding))
    except Exception as exc:
        results.append(ComplianceCheckResult(..., compliant=False, error=str(exc)))
```

`ComplianceReport` is constructed after all checks with counts derived from `results`.

---

## `find_world_writable_files()` and `find_suid_sgid_files()` Logic

Both methods iterate `search_paths`, run the appropriate `find` command with `elevated=True`,
collect stdout lines, sort and deduplicate across all paths, and return the combined list.
A non-zero exit code for any path raises `SecurityQueryError`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── securitycompliance.py   ← SecurityComplianceManager, models, config, exception

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_securitycompliance.py
```

---

## Testing Strategy

- `MockCommandRunner` records all invocations and returns configurable
  `(exit_code, stdout, stderr)` tuples keyed by command prefix list.
- `MockFileOperator` records `read_text` and `write_text` calls with their content.
- `check_selinux` tests supply canned `getenforce` and `sestatus` stdout for each mode.
- `check_file_permissions` tests supply canned `stat` output for compliant and
  non-compliant cases, and a "No such file" error for the absent-file branch.
- `check_ssh_config` tests supply canned `sshd_config` content with active, commented-out,
  and absent settings.
- `apply_ssh_hardening` tests assert the written content contains the replaced and
  appended lines, and that `systemctl restart sshd` is called afterward.
- `find_world_writable_files` and `find_suid_sgid_files` tests supply multi-line `find`
  stdout and assert deduplication and sorting.
- `run_compliance_checks` tests verify dispatch to each check type and exception capture.
- All dry-run tests assert zero `write_text` calls and zero mutating command invocations.
- No test requires file system access or live command execution.
