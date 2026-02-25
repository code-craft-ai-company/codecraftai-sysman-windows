# Implementation Plan

- [ ] 1. Create the `filesystemmanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/filesystemmanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 2. Define status enum and action result dataclass
  - Implement `FilesystemActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`,
    `DRY_RUN`
  - Implement `FilesystemActionResult` dataclass with fields: `status`, `path`, `exit_code`,
    `stdout`, `stderr`, `command`, `detail`
  - _Requirements: 1, 2, 3, 4, 7, 8, 10, 11_

- [ ] 3. Define query result dataclasses
  - Implement `PathInfo` dataclass with fields: `path`, `path_type`, `mode`, `owner`,
    `group`, `size_bytes`
  - Implement `DiskUsageInfo` dataclass with fields: `path`, `total_bytes`, `used_bytes`,
    `available_bytes`, `use_percent`
  - Implement `MountInfo` dataclass with fields: `device`, `mountpoint`, `fstype`,
    `options`
  - _Requirements: 5, 6, 9_

- [ ] 4. Define input spec dataclasses
  - Implement `MountSpec` with fields: `device`, `mountpoint`, `fstype`, `options`
  - Implement `FstabEntry` with fields: `device`, `mountpoint`, `fstype`, `options`,
    `dump`, `passno`
  - _Requirements: 7, 10_

- [ ] 5. Define typed exceptions
  - Implement `FilesystemQueryError` with `exit_code` and `stderr` attributes
  - _Requirements: 5, 6, 9_

- [ ] 6. Implement `FilesystemManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - Add `fstab_path: str = "/etc/fstab"` field
  - _Requirements: 1.7, 2.6, 3.7, 4.6, 7.7, 8.6, 10.5, 11.5, 12_

- [ ] 7. Implement `FilesystemManager.__init__` and `query_path()`
  - Accept `ICommandRunner` and `FilesystemManagerConfig` as constructor arguments
  - Implement `query_path(path)` using `stat -c "%F %a %U %G %s" <path>`
  - Parse the space-delimited stat output into a `PathInfo` record
  - Return `None` when stat exits non-zero (path not found)
  - Raise `FilesystemQueryError` on unexpected non-zero exit codes
  - _Requirements: 5_

- [ ] 8. Implement `create_directory()`
  - Call `query_path()` to check existence; return `SKIPPED` if directory already exists
  - Build `mkdir -p <path>` command; pass `elevated` flag
  - After successful mkdir: invoke `chmod` if `mode` provided; invoke `chown` if
    `owner` or `group` provided
  - Honour `dry_run` flag throughout
  - _Requirements: 1_

- [ ] 9. Implement `remove_directory()`
  - Call `query_path()` to check existence; return `SKIPPED` if path does not exist
  - Build `rmdir <path>` when `recursive=False`; build `rm -rf <path>` when `recursive=True`
  - Pass `elevated` flag; honour `dry_run` flag
  - _Requirements: 2_

- [ ] 10. Implement private `_read_symlink()` helper
  - Run `readlink <path>` via runner (no elevation — read-only)
  - Return the stdout string (resolved target) on exit 0
  - Return `None` on non-zero exit (path absent or not a symlink)
  - _Requirements: 3, 4_

- [ ] 11. Implement `create_symlink()`
  - Call `query_path()` to check what (if anything) exists at the link path
  - When nothing exists: build `ln -s <target> <link>`; pass `elevated` flag
  - When a symlink exists: call `_read_symlink()` to get current target
    - If target matches: return `SKIPPED`
    - If target differs: run `unlink <link>` then `ln -s <target> <link>` (both elevated)
  - When a non-symlink exists: return `FAILED` with conflict detail message
  - Honour `dry_run` flag; return `DRY_RUN` if active
  - _Requirements: 3_

- [ ] 12. Implement `remove_symlink()`
  - Call `query_path()` to check what exists at the path
  - When path absent: return `SKIPPED`
  - When path type is not `symbolic link`: return `FAILED` with conflict detail message
  - Build `unlink <path>`; pass `elevated` flag; honour `dry_run` flag
  - _Requirements: 4_

- [ ] 13. Implement `query_disk_usage()`
  - Build `df -B1 --output=size,used,avail,pcent <path>` command (no elevation)
  - Parse the second output line into a `DiskUsageInfo` record
  - Strip the `%` character from the `pcent` field and convert to integer
  - Raise `FilesystemQueryError` on non-zero exit code
  - _Requirements: 6_

- [ ] 14. Implement `list_mounts()`
  - Build `findmnt --json` command (no elevation)
  - Parse the JSON output; extract the `filesystems` array
  - Map each entry to a `MountInfo` record
  - Return empty list when no filesystems are present
  - Raise `FilesystemQueryError` on non-zero exit code
  - _Requirements: 9_

- [ ] 15. Implement `mount()`
  - Call `list_mounts()` to check current mount state
  - Return `SKIPPED` if device is already mounted at the requested mountpoint
  - Build `mount` command from `MountSpec`; include `-t` and `-o` when set
  - Pass `elevated` flag; honour `dry_run` flag
  - _Requirements: 7_

- [ ] 16. Implement `unmount()`
  - Call `list_mounts()` to check current mount state
  - Return `SKIPPED` if mountpoint is not currently in the mount list
  - Build `umount [-l] <mountpoint>` command; pass `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 8_

- [ ] 17. Implement private `_read_fstab()` helper
  - Run `cat <fstab_path>` via runner (no elevation)
  - Return the stdout string; return empty string when cat exits non-zero
  - _Requirements: 10, 11_

- [ ] 18. Implement private `_parse_fstab()` helper
  - Accept fstab content string
  - Parse line-by-line; skip comment lines (`#`) and blank lines
  - Return list of six-field tuples: `(device, mountpoint, fstype, options, dump, passno)`
  - _Requirements: 10, 11_

- [ ] 19. Implement `add_fstab_entry()`
  - Call `_read_fstab()` then `_parse_fstab()` to get current entries
  - Return `SKIPPED` if any existing entry matches on device or mountpoint
  - Render `FstabEntry` as a tab-separated six-field line
  - Append the line to the current fstab content; write via `tee` with `elevated` flag
  - Honour `dry_run` flag; return `DRY_RUN` with the entry that would be appended
  - _Requirements: 10_

- [ ] 20. Implement `remove_fstab_entry()`
  - Call `_read_fstab()` then `_parse_fstab()` to get current entries
  - Return `SKIPPED` if no entry matches the supplied device-or-mountpoint string
  - Remove matching lines from content; write updated content via `tee` with `elevated` flag
  - Honour `dry_run` flag
  - _Requirements: 11_

- [ ] 21. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_filesystemmanagement.py`
  - Implement `MockCommandRunner` keyed on command args prefix
  - Helper `make_stat_output(path_type, mode, owner, group, size)` for stat mock responses
  - Helper `make_findmnt_json(mounts)` for `findmnt --json` mock responses
  - Helper `make_df_output(size, used, avail, pcent)` for df mock responses
  - Override `fstab_path` in all fstab tests
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 22. Write tests for `query_path()`
  - Test: path exists as directory — returns `PathInfo` with correct fields
  - Test: path exists as regular file — `path_type` is `regular file`
  - Test: path exists as symlink — `path_type` is `symbolic link`
  - Test: path absent — returns `None`
  - Test: unexpected stat error — raises `FilesystemQueryError`
  - _Requirements: 5_

- [ ] 23. Write tests for `create_directory()`
  - Test: directory absent — `mkdir` issued, returns `SUCCESS`
  - Test: directory already exists — returns `SKIPPED`, no mkdir issued
  - Test: with `mode` — `chmod` called after `mkdir`
  - Test: with `owner` and `group` — `chown` called after `mkdir`
  - Test: `mkdir` fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`, no commands issued
  - _Requirements: 1_

- [ ] 24. Write tests for `remove_directory()`
  - Test: directory exists, `recursive=False` — `rmdir` issued, returns `SUCCESS`
  - Test: directory exists, `recursive=True` — `rm -rf` issued, returns `SUCCESS`
  - Test: directory absent — returns `SKIPPED`
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 2_

- [ ] 25. Write tests for `create_symlink()`
  - Test: link path absent — `ln -s` issued, returns `SUCCESS`
  - Test: symlink exists with same target — returns `SKIPPED`
  - Test: symlink exists with different target — `unlink` then `ln -s` issued
  - Test: non-symlink exists at link path — returns `FAILED` with conflict detail
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 3_

- [ ] 26. Write tests for `remove_symlink()`
  - Test: symlink exists — `unlink` issued, returns `SUCCESS`
  - Test: path absent — returns `SKIPPED`
  - Test: path is not a symlink — returns `FAILED` with conflict detail
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 4_

- [ ] 27. Write tests for `query_disk_usage()`
  - Test: valid mount point — returns `DiskUsageInfo` with correct byte values
  - Test: non-mount-point path — returns usage of containing filesystem
  - Test: command fails — raises `FilesystemQueryError`
  - _Requirements: 6_

- [ ] 28. Write tests for `list_mounts()`, `mount()`, and `unmount()`
  - Test list_mounts: JSON parsed into `MountInfo` list
  - Test list_mounts: command fails — raises `FilesystemQueryError`
  - Test mount: device not mounted — `mount` command issued, returns `SUCCESS`
  - Test mount: device already mounted at mountpoint — returns `SKIPPED`
  - Test mount: with `fstype` and `options` — flags included in command
  - Test mount: dry-run — returns `DRY_RUN`
  - Test unmount: mountpoint is mounted — `umount` issued, returns `SUCCESS`
  - Test unmount: mountpoint not mounted — returns `SKIPPED`
  - Test unmount: `lazy=True` — `-l` flag present in command
  - Test unmount: dry-run — returns `DRY_RUN`
  - _Requirements: 7, 8, 9_

- [ ] 29. Write tests for `add_fstab_entry()` and `remove_fstab_entry()`
  - Test add: no existing entry — entry line appended, file written via tee
  - Test add: matching device already present — returns `SKIPPED`
  - Test add: matching mountpoint already present — returns `SKIPPED`
  - Test add: dry-run — returns `DRY_RUN` with entry detail
  - Test remove: matching entry exists — line removed, file written via tee
  - Test remove: no matching entry — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 10, 11_
