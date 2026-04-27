# Tasks

## Pilot Scope

The pilot exposes seven verbs (`add_forwarder`,
`remove_forwarder`, `list_forwarders`, `enable_forwarder`,
`disable_forwarder`, `test_forwarder`, `get_forwarder_status`)
through a single `WindowsRemoteLogForwardingManager`.  Every
verb is dispatched through the injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` describing the ten requirements
      that drive the implementation, including the WEF model
      and the WEF-vs-syslog distinction.
- [x] Author `design.md` describing the architecture, the
      subscription XML rendering, the WEF-vs-syslog gap, and
      the documented contract drift.
- [x] Author `remotelogforwarding.py` with:
    - [x] `WindowsForwarderActionStatus` (alias of
          `ActionStatus`).
    - [x] `WindowsForwarderManagerConfig` dataclass.
    - [x] `WindowsForwarderActionResult` dataclass.
    - [x] `WindowsForwarderSpec` dataclass.
    - [x] `WindowsForwarderInfo` dataclass.
    - [x] `WindowsForwarderStatus` dataclass.
    - [x] `WindowsForwarderTestResult` dataclass.
    - [x] `WindowsForwarderQueryError` exception.
    - [x] `WindowsRemoteLogForwardingManager` class with the
          seven verbs.
    - [x] Module docstring documenting the WEF-vs-syslog
          distinction prominently.
    - [x] Module-level renderer
          `_render_subscription_xml`.
    - [x] Module-level parser `_parse_wecutil_gs_output`.
- [x] Author the test module with at least 14 tests covering:
    - [x] `add_forwarder` argv shape.
    - [x] `add_forwarder` SKIPPED when present with matching
          XML.
    - [x] `add_forwarder` DRY_RUN does not invoke the runner.
    - [x] `remove_forwarder` argv shape.
    - [x] `remove_forwarder` SKIPPED when absent.
    - [x] `list_forwarders` parser handles multiple
          subscriptions.
    - [x] `enable_forwarder` argv shape.
    - [x] `enable_forwarder` SKIPPED when already enabled.
    - [x] `disable_forwarder` argv shape.
    - [x] `test_forwarder` invokes Write-EventLog scriptblock.
    - [x] `get_forwarder_status` parses output.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] WEC subscription XML generation matches schema.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **Syslog-protocol forwarding.** Use Winlogbeat / NXLog.
- **Collector-Initiated mode.** Only Source-Initiated is
  rendered.
- **Source-side WinRM listener configuration.** Belongs to
  Group Policy / DSC.
- **Channel ACL adjustments via `wevtutil sl <channel> /ca:`.**
- **Full `RuntimeStatus` parsing for `last_event_time` /
  `error_count` / `bytes_sent`.**

---

## Acceptance Snapshot

`requirements.md` covers ten requirements.  `design.md`
documents the WEF model, the syslog gap, the subscription XML
rendering, and the spec-vs-contract drift.  The implementation
file passes the following hand-checks:

1. Every public method has a Sphinx docstring with
   `:param`, `:returns`, and (where applicable) `:raises`.
2. Every renderer / parser is a module-level pure function
   with keyword-only parameters.
3. No nested function calls in the dispatch path.
4. No mutable default values.
5. No truthy / falsy shortcut tests.
6. Every error string is built via `os.linesep.join(lines)`.
7. The module docstring repeats the WEF-vs-syslog limitation.

---

## Test Bar

- Minimum 14 tests; current count at completion: 16.
- Every test uses `_MockCommandRunner` — no live
  `wecutil.exe` / `powershell.exe` invocations.
