# Implementation Plan

- [ ] 1. Create the `repositorymanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/repositorymanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9_

- [ ] 2. Define status enum and result dataclasses
  - Implement `RepoActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `RepoActionResult` dataclass with fields: `status`, `repo_id`, `exit_code`,
    `stdout`, `stderr`, `command`, `file_path`
  - Implement `RepoInfo` dataclass with fields: `repo_id`, `name`, `enabled`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8_

- [ ] 3. Define `RepoDefinition` input dataclass
  - Add fields: `repo_id`, `name`, `base_url`, `mirror_list`, `enabled`, `gpg_check`,
    `gpg_key`, `priority`
  - Mark `base_url` and `mirror_list` as mutually exclusive via `__post_init__` validation
  - _Requirements: 1_

- [ ] 4. Define typed exceptions
  - Implement `RepoQueryError` with `exit_code` and `stderr` attributes
  - Implement `RepoNotFoundError` with `repo_id` and `expected_path` attributes
  - Implement `RepoKeyFileNotFoundError` with `filepath` attribute
  - _Requirements: 6, 7, 8_

- [ ] 5. Implement `RepositoryManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - Add `repos_dir: str = "/etc/yum.repos.d"` field
  - _Requirements: 1.6, 2.5, 3.6, 4.6, 5.5, 7.5, 8.6, 9_

- [ ] 6. Implement `RepositoryManager.__init__` and `list_repos()`
  - Accept `ICommandRunner` and `RepositoryManagerConfig` as constructor arguments
  - Implement `list_repos()` using `dnf repolist all`
  - Parse dnf output lines into a list of `RepoInfo` records
  - Return empty list when no repositories are configured
  - Raise `RepoQueryError` on non-zero exit code
  - _Requirements: 6_

- [ ] 7. Implement `.repo` file renderer
  - Implement a private `_render_repo_file(repo_def: RepoDefinition) -> str` method
  - Render all set fields from `RepoDefinition` into INI-style `.repo` file content
  - Omit optional fields that are `None`
  - _Requirements: 1_

- [ ] 8. Implement `add()`
  - Build the target file path from `repos_dir` and `repo_id`
  - Read existing file content via `cat <filepath>` through the runner (if file exists)
  - Compare rendered content to existing content; return `SKIPPED` if identical
  - Write file via `tee <filepath>` through the runner with rendered content on stdin
  - Honour `dry_run` flag; return `DRY_RUN` result without calling runner
  - Pass `elevated` flag to runner for the write command
  - _Requirements: 1_

- [ ] 9. Implement `remove()`
  - Build the target file path from `repos_dir` and `repo_id`
  - Check file existence via runner; return `SKIPPED` if file does not exist
  - Issue `rm -f <filepath>` via the runner
  - Honour `dry_run` flag
  - Pass `elevated` flag to runner
  - _Requirements: 2_

- [ ] 10. Implement `enable()`
  - Call `list_repos()` to determine current enabled state
  - Return `SKIPPED` if repository is already enabled
  - Return `FAILED` if repository ID is not found in the list
  - Invoke `dnf config-manager --set-enabled <repo-id>` via the runner
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 3_

- [ ] 11. Implement `disable()`
  - Call `list_repos()` to determine current enabled state
  - Return `SKIPPED` if repository is already disabled
  - Return `FAILED` if repository ID is not found in the list
  - Invoke `dnf config-manager --set-disabled <repo-id>` via the runner
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 4_

- [ ] 12. Implement `refresh()`
  - When `repo_id` is `None`: run `dnf clean metadata` then `dnf makecache`
  - When `repo_id` is set: run scoped clean and makecache with `--disablerepo="*"
    --enablerepo=<repo-id>` on both commands
  - Return `FAILED` on the first command that fails, capturing its exit code and stderr
  - Return `SUCCESS` only when both commands succeed
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 5_

- [ ] 13. Implement `set_priority()`
  - Build the target file path from `repos_dir` and `repo_id`
  - Raise `RepoNotFoundError` if the `.repo` file does not exist
  - Read current `priority=` value from file via runner; return `SKIPPED` if already correct
  - Update the `priority=` field using `dnf config-manager --setopt=<id>.priority=<n> --save`
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 7_

- [ ] 14. Implement `import_gpg_key()`
  - Accept either a URL string or a local file path string as `key_source`
  - When `key_source` is a local path: verify file exists before calling runner;
    raise `RepoKeyFileNotFoundError` if not found
  - Invoke `rpm --import <key_source>` via the runner
  - Honour `dry_run` flag; pass `elevated` flag to runner
  - _Requirements: 8_

- [ ] 15. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_repositorymanagement.py`
  - Implement `MockCommandRunner` that returns configurable `RunResult` values
  - Override `repos_dir` in `RepositoryManagerConfig` to a temp path in all tests
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9_

- [ ] 16. Write tests for `list_repos()`
  - Test: repos returned — output parsed into correct `RepoInfo` list
  - Test: no repos configured — returns empty list
  - Test: command failure — raises `RepoQueryError`
  - _Requirements: 6_

- [ ] 17. Write tests for `add()`
  - Test: new repo — runner called with tee command and rendered content, returns `SUCCESS`
  - Test: existing repo with identical content — returns `SKIPPED`, no write issued
  - Test: existing repo with different content — file overwritten, returns `SUCCESS`
  - Test: write failure — returns `FAILED`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called for write
  - Test: elevated mode — runner called with `elevated=True`
  - _Requirements: 1_

- [ ] 18. Write tests for `remove()`
  - Test: file exists — rm command issued, returns `SUCCESS`
  - Test: file does not exist — returns `SKIPPED`, no command issued
  - Test: removal failure — returns `FAILED`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 2_

- [ ] 19. Write tests for `enable()` and `disable()`
  - Test: enable a disabled repo — config-manager called, returns `SUCCESS`
  - Test: enable an already-enabled repo — returns `SKIPPED`
  - Test: enable unknown repo — returns `FAILED`
  - Test: disable an enabled repo — config-manager called, returns `SUCCESS`
  - Test: disable an already-disabled repo — returns `SKIPPED`
  - Test: dry-run mode on both — returns `DRY_RUN`, runner not called
  - _Requirements: 3, 4_

- [ ] 20. Write tests for `refresh()`
  - Test: all repos, both commands succeed — returns `SUCCESS`
  - Test: all repos, first command fails — returns `FAILED` with first command's stderr
  - Test: scoped refresh, both commands use correct repo flags — returns `SUCCESS`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 5_

- [ ] 21. Write tests for `set_priority()`
  - Test: file exists, priority changes — config-manager called, returns `SUCCESS`
  - Test: file exists, priority unchanged — returns `SKIPPED`, no write issued
  - Test: file does not exist — raises `RepoNotFoundError`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 7_

- [ ] 22. Write tests for `import_gpg_key()`
  - Test: URL key source — runner called with correct args, returns `SUCCESS`
  - Test: local file path, file exists — runner called, returns `SUCCESS`
  - Test: local file path, file missing — raises `RepoKeyFileNotFoundError` before runner called
  - Test: import failure — returns `FAILED` with exit code and stderr
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 8_
