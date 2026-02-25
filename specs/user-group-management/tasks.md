# Implementation Plan

- [ ] 1. Create the `usergroupmanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/usergroupmanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 2. Define `ActionStatus` enum and result dataclasses
  - Implement `ActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `UserActionResult` dataclass with fields: `status`, `username`, `exit_code`,
    `stdout`, `stderr`, `command`
  - Implement `GroupActionResult` dataclass with fields: `status`, `groupname`, `exit_code`,
    `stdout`, `stderr`, `command`
  - _Requirements: 1, 2, 3, 5, 6, 7, 8, 9, 10, 11_

- [ ] 3. Define query result dataclasses
  - Implement `UserInfo` dataclass with fields: `username`, `uid`, `gid`, `comment`,
    `home`, `shell`, `locked`
  - Implement `GroupInfo` dataclass with fields: `groupname`, `gid`, `members`
  - _Requirements: 4_

- [ ] 4. Define input spec dataclasses
  - Implement `UserSpec` with fields: `username`, `uid`, `home`, `shell`,
    `primary_group`, `comment`, `system_account`
  - Implement `UserModSpec` with fields: `shell`, `home`, `move_home`, `primary_group`,
    `comment`, `uid`
  - Implement `GroupSpec` with fields: `groupname`, `gid`, `system_group`
  - Implement `PasswordAgeSpec` with fields: `max_days`, `min_days`, `expiry_date`
  - _Requirements: 1, 3, 5, 11_

- [ ] 5. Define typed exceptions
  - Implement `UserNotFoundError` with `username` attribute
  - Implement `GroupNotFoundError` with `groupname` attribute
  - Implement `UserQueryError` with `exit_code` and `stderr` attributes
  - Implement `UserKeyError` with `username` attribute
  - _Requirements: 3, 4, 7, 8, 9, 10, 11_

- [ ] 6. Implement `UserGroupManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - _Requirements: 1.11, 2.6, 3.10, 5.7, 6.5, 7.7, 8.7, 9.8, 10.8, 11.8, 12_

- [ ] 7. Implement `UserManager.__init__` and `UserManager.query()`
  - Accept `ICommandRunner` and `UserGroupManagerConfig` as constructor arguments
  - Implement `query(username)` using `getent passwd <username>`
  - Parse colon-delimited passwd entry into `UserInfo`
  - Determine `locked` state via `passwd -S <username>` — check for `L` flag in output
  - Return `None` when `getent` exits with code 2 (not found)
  - Raise `UserQueryError` on any other unexpected non-zero exit
  - _Requirements: 4_

- [ ] 8. Implement `GroupManager.__init__` and `GroupManager.query()`
  - Accept `ICommandRunner` and `UserGroupManagerConfig` as constructor arguments
  - Implement `query(groupname)` using `getent group <groupname>`
  - Parse colon-delimited group entry into `GroupInfo` including members list
  - Return `None` when `getent` exits with code 2 (not found)
  - _Requirements: 5_

- [ ] 9. Implement `UserManager.create()`
  - Call `query()` to check existence; return `SKIPPED` if user already exists
  - Build `useradd` args from `UserSpec` fields, omitting flags for `None` values
  - Pass `-r` when `system_account=True`
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 1_

- [ ] 10. Implement `UserManager.delete()`
  - Call `query()` to check existence; return `SKIPPED` if user does not exist
  - Build `userdel` args; pass `-r` when `remove_home=True`
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 2_

- [ ] 11. Implement `UserManager.modify()`
  - Call `query()`; raise `UserNotFoundError` if user does not exist
  - Build `usermod` args from non-`None` fields in `UserModSpec`
  - Pass both `-d` and `-m` when `home` is set and `move_home=True`
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 3_

- [ ] 12. Implement `UserManager.lock()` and `UserManager.unlock()`
  - Call `query()`; raise `UserNotFoundError` if absent
  - For `lock()`: return `SKIPPED` if `UserInfo.locked` is `True`
  - For `unlock()`: return `SKIPPED` if `UserInfo.locked` is `False`
  - Build `usermod -L` or `usermod -U` command
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 10_

- [ ] 13. Implement `GroupManager.create()`
  - Call `query()` to check existence; return `SKIPPED` if group already exists
  - Build `groupadd` args from `GroupSpec`, pass `-r` when `system_group=True`
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 5_

- [ ] 14. Implement `GroupManager.delete()`
  - Call `query()` to check existence; return `SKIPPED` if group does not exist
  - Build `groupdel <groupname>` command
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 6_

- [ ] 15. Implement `UserManager.add_to_group()` and `UserManager.remove_from_group()`
  - For both: call `UserManager.query()` — raise `UserNotFoundError` if absent
  - For both: call `GroupManager.query()` — raise `GroupNotFoundError` if absent
  - For `add_to_group()`: check `GroupInfo.members`; return `SKIPPED` if user already
    in members list; build `usermod -aG <groupname> <username>`
  - For `remove_from_group()`: check `GroupInfo.members`; return `SKIPPED` if user not
    in members; build `gpasswd -d <username> <groupname>`
  - Honour `dry_run` flag; pass `elevated` flag to runner on both
  - _Requirements: 7, 8_

- [ ] 16. Implement `UserManager.add_ssh_key()` and `UserManager.remove_ssh_key()`
  - Raise `UserKeyError` if `public_key` is empty or whitespace-only
  - Call `query()`; raise `UserNotFoundError` if absent; extract home directory
  - Check `.ssh` directory existence via `stat`; create with correct mode and ownership
    if missing (mkdir, chmod, chown sequence — all elevated)
  - Read current `authorized_keys` via `cat` (empty string if file absent)
  - For `add_ssh_key()`: return `SKIPPED` if key line already present; append key line
    to content; write via `tee` with stdin; set mode and ownership on new file if created
  - For `remove_ssh_key()`: return `SKIPPED` if key line not found; remove matching line;
    write updated content via `tee` with stdin
  - Honour `dry_run` flag on all write steps
  - _Requirements: 9_

- [ ] 17. Implement `UserManager.expire_password()` and `UserManager.set_password_age()`
  - For both: call `query()`; raise `UserNotFoundError` if absent
  - For `expire_password()`: build `passwd --expire <username>`
  - For `set_password_age()`: build one `chage` call per non-`None` field in
    `PasswordAgeSpec` (`-M`, `-m`, `-E`); execute each command sequentially
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 11_

- [ ] 18. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_usergroupmanagement.py`
  - Implement `MockCommandRunner` keyed on command args prefix
  - Helper `make_passwd_entry(username, ...)` to produce `getent passwd` output strings
  - Helper `make_group_entry(groupname, members)` to produce `getent group` output strings
  - Helper `make_passwd_status(username, locked)` to produce `passwd -S` output strings
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12_

- [ ] 19. Write tests for `UserManager.query()`
  - Test: user exists — returns populated `UserInfo`
  - Test: user absent (`getent` exit 2) — returns `None`
  - Test: locked account — `UserInfo.locked` is `True`
  - Test: unlocked account — `UserInfo.locked` is `False`
  - Test: unexpected exit code — raises `UserQueryError`
  - _Requirements: 4_

- [ ] 20. Write tests for `GroupManager.query()`
  - Test: group exists with members — returns `GroupInfo` with members list
  - Test: group exists with no members — returns `GroupInfo` with empty list
  - Test: group absent — returns `None`
  - _Requirements: 5 (query path)_

- [ ] 21. Write tests for `UserManager.create()`
  - Test: user absent — `useradd` called with correct args, returns `SUCCESS`
  - Test: all `UserSpec` fields set — correct flags passed to `useradd`
  - Test: `system_account=True` — `-r` flag present in command
  - Test: user already exists — returns `SKIPPED`, no command issued
  - Test: `useradd` fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`, no command issued
  - _Requirements: 1_

- [ ] 22. Write tests for `UserManager.delete()`
  - Test: user exists — `userdel` called, returns `SUCCESS`
  - Test: `remove_home=True` — `-r` flag present
  - Test: user absent — returns `SKIPPED`
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 2_

- [ ] 23. Write tests for `UserManager.modify()`
  - Test: all `UserModSpec` fields — correct flags assembled in command
  - Test: `move_home=True` with `home` set — both `-d` and `-m` present
  - Test: user absent — raises `UserNotFoundError`
  - Test: `usermod` fails — returns `FAILED`
  - Test: dry-run — returns `DRY_RUN`
  - _Requirements: 3_

- [ ] 24. Write tests for `UserManager.lock()` and `UserManager.unlock()`
  - Test lock: unlocked account — `usermod -L` issued, returns `SUCCESS`
  - Test lock: already locked — returns `SKIPPED`
  - Test lock: user absent — raises `UserNotFoundError`
  - Test lock: dry-run — returns `DRY_RUN`
  - Test unlock: locked account — `usermod -U` issued, returns `SUCCESS`
  - Test unlock: already unlocked — returns `SKIPPED`
  - Test unlock: dry-run — returns `DRY_RUN`
  - _Requirements: 10_

- [ ] 25. Write tests for `GroupManager.create()` and `GroupManager.delete()`
  - Test create: group absent — `groupadd` called, returns `SUCCESS`
  - Test create: `system_group=True` — `-r` flag present
  - Test create: group exists — returns `SKIPPED`
  - Test create: dry-run — returns `DRY_RUN`
  - Test delete: group exists — `groupdel` called, returns `SUCCESS`
  - Test delete: group absent — returns `SKIPPED`
  - Test delete: dry-run — returns `DRY_RUN`
  - _Requirements: 5, 6_

- [ ] 26. Write tests for `add_to_group()` and `remove_from_group()`
  - Test add: user not in group — `usermod -aG` issued, returns `SUCCESS`
  - Test add: user already in group — returns `SKIPPED`
  - Test add: user absent — raises `UserNotFoundError`
  - Test add: group absent — raises `GroupNotFoundError`
  - Test add: dry-run — returns `DRY_RUN`
  - Test remove: user in group — `gpasswd -d` issued, returns `SUCCESS`
  - Test remove: user not in group — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 7, 8_

- [ ] 27. Write tests for `add_ssh_key()` and `remove_ssh_key()`
  - Test add: key absent, `.ssh` and file exist — key appended, returns `SUCCESS`
  - Test add: key already present — returns `SKIPPED`, no write issued
  - Test add: `.ssh` dir missing — mkdir/chmod/chown sequence issued before write
  - Test add: `authorized_keys` missing — file created with correct mode and ownership
  - Test add: empty key string — raises `UserKeyError`
  - Test add: user absent — raises `UserNotFoundError`
  - Test add: dry-run — returns `DRY_RUN`, no write issued
  - Test remove: key present — key line removed, file rewritten
  - Test remove: key absent — returns `SKIPPED`
  - Test remove: dry-run — returns `DRY_RUN`
  - _Requirements: 9_

- [ ] 28. Write tests for `expire_password()` and `set_password_age()`
  - Test expire: `passwd --expire` issued, returns `SUCCESS`
  - Test expire: user absent — raises `UserNotFoundError`
  - Test expire: dry-run — returns `DRY_RUN`
  - Test set age: `max_days` only — single `chage -M` command issued
  - Test set age: `min_days` only — single `chage -m` command issued
  - Test set age: `expiry_date` only — single `chage -E` command issued
  - Test set age: all fields — three sequential commands issued
  - Test set age: dry-run — returns `DRY_RUN`
  - _Requirements: 11_
