# Implementation Plan — Windows Precision-Time Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify follow-ups deferred to a later iteration.

## Module and Models

- [x] 1. Create the
  `automojo.sysman.windows.precisiontimemanagement` module.

- [x] 2. Define `WindowsPrecisionTimeActionStatus` (alias of
  `ActionStatus`).

- [x] 3. Define `WindowsPrecisionTimeManagerConfig` dataclass with
  `dry_run`, `elevated`, `service_name`.

- [x] 4. Define `WindowsPrecisionTimeActionResult` dataclass with
  the `server` carry-through field.

- [x] 5. Define `WindowsTimeStatus`, `WindowsTimeSourceInfo`,
  `WindowsTimeDrift` dataclasses.

- [x] 6. Define `WindowsPrecisionTimeQueryError` exception.

## WindowsPrecisionTimeManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config.
  Spec deviation: no `IFileOperator` collaborator — every external
  call routes through the runner.

- [x] 8. Implement `get_time_status()` — dispatches
  `w32tm /query /status /verbose` and parses the keyed-line
  payload.  Inactive-service stderr signatures degrade to
  `enabled=False` rather than raise.

- [x] 9. Implement `get_time_sources()` — dispatches
  `w32tm /query /peers`, splits on blank-line stanza separators,
  parses each peer block.

- [x] 10. Implement `get_drift()` — reads current peer via
  `get_time_status`, dispatches
  `w32tm /stripchart /computer:<peer> /samples:1 /dataonly`,
  parses the trailing offset token.

- [x] 11. Implement `add_time_source(server)` — recompute peer
  list, dispatch `w32tm /config /manualpeerlist:"<list>"
  /syncfromflags:manual /update` plus service-bounce.

- [x] 12. Implement `remove_time_source(server)` — recompute
  list, same `/config /update` + service-bounce.

- [x] 13. Implement `set_time_servers(servers)` — render full
  list, same `/config /update` + service-bounce.

- [x] 14. Implement `force_sync_now()` — dispatch
  `w32tm /resync /force`.

- [x] 15. Implement `enable_sync()` / `disable_sync()` — chain
  `sc config <service> start= auto|disabled` with
  `net start|stop <service>`; idempotent via the
  `sc query <service>` reading.

## Renderers and Parsers — Implemented

- [x] 16. `_parse_w32tm_status` — keyed-line parser for the
  `w32tm /query /status /verbose` payload.

- [x] 17. `_parse_w32tm_peers` — blank-line stanza splitter plus
  per-stanza keyed-line parser.

- [x] 18. `_classify_w32tm_state` — map the W32Time `State:`
  vocabulary onto the contract's
  selected/candidate/unreachable terms.

- [x] 19. `_split_into_stanzas` — blank-line stanza splitter.

- [x] 20. `_parse_optional_int_first_token` — parse the leading
  integer token from a `Stratum: 2 (description)` field.

- [x] 21. `_parse_optional_seconds_field` — parse the
  `<num>s`-suffixed seconds field used by `Phase Offset`.

- [x] 22. `_parse_w32tm_stripchart_offset` — extract the offset
  token from a `/stripchart /dataonly` line.

- [x] 23. `_stderr_indicates_inactive` — heuristic for the
  `0x80070426` "service has not been started" stderr signatures.

## Tests

- [x] 24. `test_get_time_status_parses_w32tm_output`
- [x] 25. `test_get_time_status_returns_inactive_when_w32time_down`
- [x] 26. `test_get_time_sources_parses_w32tm_output`
- [x] 27. `test_get_drift_parses_w32tm_output`
- [x] 28. `test_add_time_source_invokes_w32tm`
- [x] 29. `test_add_time_source_skipped_when_present`
- [x] 30. `test_add_time_source_dry_run`
- [x] 31. `test_remove_time_source_invokes_w32tm`
- [x] 32. `test_remove_time_source_skipped_when_absent`
- [x] 33. `test_set_time_servers_invokes_w32tm`
- [x] 34. `test_set_time_servers_skipped_when_match`
- [x] 35. `test_force_sync_now_invokes_w32tm`
- [x] 36. `test_enable_sync_skipped_when_already_enabled`
- [x] 37. `test_disable_sync_invokes_net_stop`
- [x] 38. `test_writes_elevate`
- [x] 39. `test_reads_never_elevate`
- [x] 40. `test_failed_command_surfaces_stderr`
- [x] 41. `test_get_time_status_raises_on_unexpected_w32tm_failure`

## Follow-ups

- [ ] 42. Replace string `last_sync` with a parsed `datetime`
  once a shared locale/timezone-handling helper lands in the
  package.
- [ ] 43. Surface a unified diff on dry-run results so callers
  can audit the would-have-been content change.
- [ ] 44. Extend `get_time_sources` to consult the registry-
  backed parameter list under
  `HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Parameters`
  for fully-detached telemetry — current implementation reads
  via `w32tm` only.
- [ ] 45. Add a high-accuracy / PTP-mode tuning surface as an
  extension once the core manager stabilises.
