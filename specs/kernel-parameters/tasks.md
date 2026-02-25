# Implementation Plan

- [ ] 1. Create the `kernelparameters` module file
  - Create `source/packages/automojo/sysman/linux/windows/kernelparameters.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 2. Define kernel parameter enums and dataclasses
  - Implement `KernelParamActionStatus` enum: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `KernelParamInfo` dataclass with fields: `key`, `value`
  - Implement `KernelParamActionResult` dataclass with fields: `status`, `key`,
    `current_value`, `applied_value`, `filename`, `diff`, `exit_code`, `stdout`,
    `stderr`, `command`
  - _Requirements: 1, 2, 3, 4, 5, 6_

- [ ] 3. Define kernel module enums and dataclasses
  - Implement `KernelModuleActionStatus` enum: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `KernelModuleInfo` dataclass with fields: `name`, `size_bytes`, `use_count`
  - Implement `KernelModuleActionResult` dataclass with fields: `status`, `module_name`,
    `filename`, `exit_code`, `stdout`, `stderr`, `command`
  - _Requirements: 7, 8, 9, 10, 11_

- [ ] 4. Define typed exceptions
  - Implement `KernelParamQueryError` with `exit_code` and `stderr` attributes
  - Implement `KernelModuleQueryError` with `exit_code` and `stderr` attributes
  - _Requirements: 1, 6, 9_

- [ ] 5. Implement `KernelManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - Add `sysctl_dir: str = "/etc/sysctl.d"` field
  - Add `modules_load_dir: str = "/etc/modules-load.d"` field
  - _Requirements: 2.5, 3.6, 4.5, 5.4, 7.6, 8.5, 10.6, 11.5, 12_

- [ ] 6. Implement `KernelParamManager.__init__` and `read()`
  - Accept `ICommandRunner` and `KernelManagerConfig` as constructor arguments
  - Implement `read(key)` using `sysctl -n <key>` (no elevation)
  - Return `KernelParamInfo` on exit 0
  - Return `None` when sysctl exits non-zero with "No such file" in stderr
  - Raise `KernelParamQueryError` on all other non-zero exits
  - _Requirements: 1_

- [ ] 7. Implement `KernelParamManager.query_all()`
  - Build `sysctl -a` command (no elevation)
  - Parse each stdout line on the first ` = ` separator
  - Skip malformed or unreadable lines silently
  - Return list of `KernelParamInfo` records
  - Raise `KernelParamQueryError` on non-zero exit
  - _Requirements: 6_

- [ ] 8. Implement `KernelParamManager.write()`
  - Call `read(key)` to get current value; return `SKIPPED` if already matches
  - Return `DRY_RUN` with current and desired values when `dry_run=True`
  - Build `sysctl -w <key>=<value>`; pass `elevated` flag
  - Map result to `KernelParamActionResult`
  - _Requirements: 2_

- [ ] 9. Implement `KernelParamManager.apply_all()`
  - Return `DRY_RUN` without calling runner when `dry_run=True`
  - Build `sysctl --system`; pass `elevated` flag
  - Map result to `KernelParamActionResult` (no key — set `key=""`)
  - _Requirements: 5_

- [ ] 10. Implement private `_read_sysctl_dropin()` helper
  - Build path: `<sysctl_dir>/<filename>.conf`
  - Run `cat <path>` via runner (no elevation)
  - Return stdout string; return empty string on non-zero exit (file absent)
  - _Requirements: 3, 4_

- [ ] 11. Implement private `_parse_sysctl_dropin()` helper
  - Accept content string
  - Return list of `(key, value, original_line)` tuples for non-comment, non-blank lines
  - Preserve comment and blank lines as `(None, None, original_line)` tuples
  - Strip inline comments from values during parsing
  - _Requirements: 3, 4_

- [ ] 12. Implement `KernelParamManager.persist()`
  - Call `_read_sysctl_dropin()` and `_parse_sysctl_dropin()`
  - Scan parsed lines for matching key
  - Return `SKIPPED` if key present with same value
  - Replace line if key present with different value; append if absent
  - Compute diff; return `DRY_RUN` with diff when `dry_run=True`
  - Write updated content via `tee <path>` with `elevated` flag
  - _Requirements: 3_

- [ ] 13. Implement `KernelParamManager.remove_persisted()`
  - Call `_read_sysctl_dropin()`; return `SKIPPED` if file absent (empty string)
  - Call `_parse_sysctl_dropin()`; return `SKIPPED` if key not found
  - Remove matching line; write updated content via `tee` with `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 4_

- [ ] 14. Implement `KernelModuleManager.__init__` and `query_loaded()`
  - Accept `ICommandRunner` and `KernelManagerConfig` as constructor arguments
  - Implement `query_loaded()` using `lsmod` (no elevation)
  - Skip header line; parse remaining lines into `KernelModuleInfo` records
  - Raise `KernelModuleQueryError` on non-zero exit
  - _Requirements: 9_

- [ ] 15. Implement `KernelModuleManager.load()`
  - Call `query_loaded()` to check current state
  - Return `SKIPPED` if module name already present in loaded list
  - Return `DRY_RUN` when `dry_run=True`
  - Build `modprobe <name>` command; append `<param>=<value>` pairs when `params` provided
  - Pass `elevated` flag; map result to `KernelModuleActionResult`
  - _Requirements: 7_

- [ ] 16. Implement `KernelModuleManager.unload()`
  - Call `query_loaded()` to check current state
  - Return `SKIPPED` if module name is not in loaded list
  - Return `DRY_RUN` when `dry_run=True`
  - Build `modprobe -r <name>`; pass `elevated` flag
  - Map result to `KernelModuleActionResult`
  - _Requirements: 8_

- [ ] 17. Implement private `_read_module_dropin()` helper
  - Build path: `<modules_load_dir>/<filename>.conf`
  - Run `cat <path>` via runner (no elevation)
  - Return stdout string; return empty string on non-zero exit (file absent)
  - _Requirements: 10, 11_

- [ ] 18. Implement `KernelModuleManager.persist_load()`
  - Call `_read_module_dropin()`
  - Scan non-comment lines for exact module name match; return `SKIPPED` if found
  - Append module name line to content (create content if file was absent)
  - Return `DRY_RUN` when `dry_run=True`
  - Write via `tee <path>` with `elevated` flag
  - _Requirements: 10_

- [ ] 19. Implement `KernelModuleManager.remove_persisted_load()`
  - Call `_read_module_dropin()`; return `SKIPPED` if file absent
  - Scan non-comment lines; return `SKIPPED` if module name not found
  - Remove matching line; write updated content via `tee` with `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 11_

- [ ] 20. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_kernelparameters.py`
  - Implement `MockCommandRunner` keyed on command args prefix
  - Helper `make_sysctl_output(key, value)` for `sysctl -n` responses
  - Helper `make_sysctl_all_output(params_dict)` for `sysctl -a` responses
  - Helper `make_lsmod_output(modules)` for `lsmod` responses
  - Override `sysctl_dir` and `modules_load_dir` in all file-operation tests
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 21. Write tests for `KernelParamManager.read()` and `query_all()`
  - Test read: key exists — returns `KernelParamInfo` with correct value
  - Test read: key absent — returns `None`
  - Test read: unexpected error — raises `KernelParamQueryError`
  - Test query_all: parses multiple `key = value` lines correctly
  - Test query_all: skips malformed lines without error
  - Test query_all: command fails — raises `KernelParamQueryError`
  - _Requirements: 1, 6_

- [ ] 22. Write tests for `KernelParamManager.write()`
  - Test: value differs — `sysctl -w` issued, returns `SUCCESS`
  - Test: value already matches — returns `SKIPPED`, no write command issued
  - Test: command fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN` with current and desired values
  - Test: elevated mode — runner called with `elevated=True`
  - _Requirements: 2_

- [ ] 23. Write tests for `KernelParamManager.persist()`
  - Test: key absent from file — line appended, file written via tee
  - Test: key present with same value — returns `SKIPPED`
  - Test: key present with different value — line replaced, file written
  - Test: file absent — content created from scratch with key line
  - Test: dry-run — returns `DRY_RUN` with diff, no write issued
  - Test: comment and blank lines preserved in output
  - _Requirements: 3_

- [ ] 24. Write tests for `KernelParamManager.remove_persisted()`
  - Test: key present — line removed, file rewritten
  - Test: key absent from file — returns `SKIPPED`
  - Test: file absent — returns `SKIPPED`
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 4_

- [ ] 25. Write tests for `KernelParamManager.apply_all()`
  - Test: `sysctl --system` issued, returns `SUCCESS`
  - Test: command fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`, no command issued
  - _Requirements: 5_

- [ ] 26. Write tests for `KernelModuleManager.query_loaded()`
  - Test: parses lsmod output into `KernelModuleInfo` list with correct fields
  - Test: header line is skipped
  - Test: command fails — raises `KernelModuleQueryError`
  - _Requirements: 9_

- [ ] 27. Write tests for `KernelModuleManager.load()` and `unload()`
  - Test load: module not loaded — `modprobe` issued, returns `SUCCESS`
  - Test load: module already loaded — returns `SKIPPED`
  - Test load: with params — params appended to modprobe command
  - Test load: command fails — returns `FAILED`
  - Test load: dry-run — returns `DRY_RUN`
  - Test unload: module loaded — `modprobe -r` issued, returns `SUCCESS`
  - Test unload: module not loaded — returns `SKIPPED`
  - Test unload: command fails — returns `FAILED`
  - Test unload: dry-run — returns `DRY_RUN`
  - _Requirements: 7, 8_

- [ ] 28. Write tests for `KernelModuleManager.persist_load()`
  - Test: module absent from file — name appended, file written
  - Test: module already present — returns `SKIPPED`
  - Test: file absent — file created with module name
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 10_

- [ ] 29. Write tests for `KernelModuleManager.remove_persisted_load()`
  - Test: module present — line removed, file rewritten
  - Test: module absent from file — returns `SKIPPED`
  - Test: file absent — returns `SKIPPED`
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 11_
