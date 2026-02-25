# Design

## Overview

The maintenance reporting feature is implemented as a single module,
`automojo.sysman.linux.windows.maintenancereporting`, that exposes a `MaintenanceReport`
class.  The coordinator instantiates `MaintenanceReport` at the start of a run, calls
`add_action` after each operation, calls `finalise` at the end, and then calls `to_json`,
`to_text`, or `write_report` to persist or display the results.

The module contains no I/O beyond the file-operator abstraction used by `write_report`.
Timestamps are obtained via a `_clock` callable (defaulting to `datetime.utcnow`) that
can be replaced in tests.  All serialisation is implemented in pure Python using the
standard `json` module and string formatting — no third-party dependencies are required.

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Maintenance Coordinator        │  (external package)
└───────────────────┬─────────────────────┘
                    │ calls
                    ▼
┌─────────────────────────────────────────┐
│          MaintenanceReport              │  automojo.sysman.linux.windows.maintenancereporting
│                                         │
│  add_action()        finalise()         │
│  summary_counts()    to_json()          │
│  to_text()           write_report()     │
│  actions_by_status() actions_by_feature()│
└───────────────────┬─────────────────────┘
                    │ delegates file I/O to
                    ▼
┌─────────────────────────────────────────┐
│        IFileOperator                    │  automojo-interfaces
│  write_text(path, content, elevated)    │
└─────────────────────────────────────────┘
```

---

## Components

### `MaintenanceReport`

**Location:** `source/packages/automojo/sysman/linux/windows/maintenancereporting.py`

| Method | Signature | Description |
|---|---|---|
| `add_action` | `(record: ActionRecord) → None` | Append an action to the report |
| `finalise` | `() → None` | Set `finished_at` and compute `overall_status` |
| `summary_counts` | `() → ReportSummary` | Count actions by status |
| `to_json` | `(indent: int \| None = 2) → str` | Serialise to JSON string |
| `to_text` | `() → str` | Serialise to human-readable plain text |
| `write_report` | `(path: str, format: str) → ReportWriteResult` | Write JSON or text to file |
| `actions_by_status` | `(*statuses: ActionStatus) → list[ActionRecord]` | Filter by status |
| `actions_by_feature` | `(feature: str) → list[ActionRecord]` | Filter by feature name |

### `MaintenanceReportConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, `write_report` does not write any file |

---

## Data Models

### `ActionStatus`

```python
class ActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### `ActionRecord`

```python
@dataclass
class ActionRecord:
    feature: str                    # e.g. "package-management"
    operation: str                  # e.g. "install_package"
    status: ActionStatus
    detail: str | None = None       # human-readable description of what happened
    error: str | None = None        # error message or captured stderr on failure
    duration_seconds: float | None = None
    timestamp: datetime | None = None   # defaults to utcnow() at construction time
```

`timestamp` is set to `datetime.utcnow()` by the `__post_init__` hook when `None` is
passed, making it effectively automatic while remaining injectable for tests.

### `ReportSummary`

```python
@dataclass
class ReportSummary:
    total: int
    succeeded: int
    failed: int
    skipped: int
    dry_run: int
```

### `ReportWriteResult`

```python
@dataclass
class ReportWriteResult:
    success: bool
    path: str
    dry_run: bool = False
    error: str | None = None
```

### `MaintenanceReport` Fields

| Field | Type | Description |
|---|---|---|
| `run_id` | `str` | Unique identifier for the run |
| `host` | `str` | Hostname or system identifier |
| `description` | `str \| None` | Human-readable description of the run's purpose |
| `started_at` | `datetime` | UTC time the report was created |
| `finished_at` | `datetime \| None` | UTC time `finalise` was last called |
| `overall_status` | `ActionStatus \| None` | Set by `finalise`; `None` until finalised |
| `actions` | `list[ActionRecord]` | Ordered list of recorded actions |

---

## `finalise()` Logic

```
if any action.status == FAILED:
    overall_status = FAILED
else:
    overall_status = SUCCESS
finished_at = utcnow()
```

This is recalculated each time `finalise()` is called, so late-added actions are always
reflected correctly.

---

## `to_json()` Serialisation

The report is converted to a plain `dict` by a private `_to_dict()` method, then passed
to `json.dumps`.

```python
def _to_dict(self) -> dict:
    return {
        "run_id": self.run_id,
        "host": self.host,
        "description": self.description,
        "started_at": self.started_at.isoformat() + "Z",
        "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        "overall_status": self.overall_status.value if self.overall_status else None,
        "summary": dataclasses.asdict(self.summary_counts()),
        "actions": [_action_to_dict(a) for a in self.actions],
    }
```

`_action_to_dict` maps an `ActionRecord` to a plain dict, converting `timestamp` to ISO
8601 and `status` to its string value.

---

## `to_text()` Format

```
═══════════════════════════════════════════════════
MAINTENANCE REPORT
═══════════════════════════════════════════════════
Run ID    : <run_id>
Host      : <host>
Desc      : <description or "(none)">
Started   : <started_at ISO 8601>
Finished  : <finished_at ISO 8601 or "in progress">
Status    : <overall_status or "in progress">

Summary   : <total> total  <succeeded> ok  <failed> failed  <skipped> skipped  <dry_run> dry-run
───────────────────────────────────────────────────
Actions
───────────────────────────────────────────────────
[SUCCESS ] package-management / install_package    installed httpd 2.4.51        0.82s
[FAILED  ] service-management / enable_service     enable httpd failed
             Error: Unit httpd.service not found
[SKIPPED ] firewall-management / add_service       http already in public zone
───────────────────────────────────────────────────
(no actions recorded)     ← only when actions list is empty
```

Each action line: `[<STATUS padded to 7>] <feature> / <operation>  <detail>  <duration>`.
Error text is indented four spaces on the next line with the prefix `Error: `.

---

## `write_report()` Logic

1. If `config.dry_run=True`: return `ReportWriteResult(success=True, path=path, dry_run=True)`.
2. Serialise: call `to_json()` when `format="json"`, `to_text()` when `format="text"`.
3. Call `file_operator.write_text(path, content)`.
4. On success: return `ReportWriteResult(success=True, path=path)`.
5. On exception: return `ReportWriteResult(success=False, path=path, error=str(e))`.

---

## Clock Injection

`MaintenanceReport.__init__` accepts an optional `_clock` parameter (a callable returning
`datetime`).  It defaults to `datetime.utcnow`.  `ActionRecord.__post_init__` accepts an
optional `_clock` parameter with the same default.  This allows tests to supply a fixed
time without monkeypatching.

---

## Module Layout

```
source/packages/automojo/sysman/linux/windows/
├── __init__.py
└── maintenancereporting.py     ← MaintenanceReport, models, config

source/testroots/testbase/
└── automojo/tests/sysman/linux/windows/
    └── test_maintenancereporting.py
```

---

## Testing Strategy

- `MockFileOperator` records `write_text` calls and their content for assertion.
- A fixed `_clock` lambda is passed to `MaintenanceReport` and `ActionRecord` in all
  tests requiring deterministic timestamps.
- `to_json` output is parsed back with `json.loads` to assert field values rather than
  string-matching.
- `to_text` output is tested by asserting that key substrings appear in the output
  (run ID, host, status labels, action lines).
- `finalise` is tested before and after adding a failing action to verify `overall_status`
  recalculation.
- No test requires file system access or live command execution.
