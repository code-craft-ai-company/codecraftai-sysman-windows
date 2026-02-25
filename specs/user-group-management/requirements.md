# Requirements

## Introduction

The user and group management feature provides the `automojo-sysman-linux-windows` package
with the ability to create, modify, and delete local users and groups on Windows Linux
systems.  It also manages group membership, SSH authorized keys, account lock state, and
password expiry.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* users or groups
to manage — it provides the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction, enabling dry-run simulation, auditing, and test mocking without
requiring a live system.

---

## Requirements

### Requirement 1: Create a Local User

**User Story:** As a maintenance coordinator, I want to create a local user account on the
system, so that the required system or service user is present for software that depends on
it.

#### Acceptance Criteria

1. WHEN the coordinator provides a username THE SYSTEM SHALL invoke `useradd` with the
   supplied options via the command runner.
2. WHEN the user already exists THE SYSTEM SHALL skip execution and return a
   `UserActionResult` with status `SKIPPED`.
3. WHEN the coordinator specifies a UID THE SYSTEM SHALL pass `-u <uid>` to `useradd`.
4. WHEN the coordinator specifies a home directory THE SYSTEM SHALL pass `-d <home>` and
   `-m` to `useradd` to create it.
5. WHEN the coordinator specifies a login shell THE SYSTEM SHALL pass `-s <shell>` to
   `useradd`.
6. WHEN the coordinator specifies a primary group THE SYSTEM SHALL pass `-g <group>` to
   `useradd`.
7. WHEN the coordinator specifies a comment (GECOS) THE SYSTEM SHALL pass `-c "<comment>"`
   to `useradd`.
8. WHEN the coordinator sets `system_account=True` THE SYSTEM SHALL pass `-r` to `useradd`
   to create a system account with no home directory and no login shell by default.
9. WHEN the `useradd` command succeeds THE SYSTEM SHALL return a `UserActionResult` with
   status `SUCCESS`.
10. WHEN the `useradd` command fails THE SYSTEM SHALL return a `UserActionResult` with
    status `FAILED`, the exit code, and the captured stderr.
11. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
    `UserActionResult` with status `DRY_RUN`.

---

### Requirement 2: Delete a Local User

**User Story:** As a maintenance coordinator, I want to delete a local user account from
the system, so that decommissioned or unwanted accounts are removed cleanly.

#### Acceptance Criteria

1. WHEN the coordinator requests deletion of a user THE SYSTEM SHALL invoke `userdel
   <username>` via the command runner.
2. WHEN the coordinator sets `remove_home=True` THE SYSTEM SHALL pass `-r` to `userdel` to
   also remove the user's home directory and mail spool.
3. WHEN the user does not exist THE SYSTEM SHALL skip execution and return a
   `UserActionResult` with status `SKIPPED`.
4. WHEN the `userdel` command succeeds THE SYSTEM SHALL return a `UserActionResult` with
   status `SUCCESS`.
5. WHEN the `userdel` command fails THE SYSTEM SHALL return a `UserActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `UserActionResult` with status `DRY_RUN`.

---

### Requirement 3: Modify a Local User

**User Story:** As a maintenance coordinator, I want to modify the properties of an existing
local user account, so that account attributes can be corrected or updated as system
requirements evolve.

#### Acceptance Criteria

1. WHEN the coordinator provides a username and one or more properties to change THE SYSTEM
   SHALL invoke `usermod` with the corresponding flags via the command runner.
2. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
3. WHEN the coordinator specifies a new login shell THE SYSTEM SHALL pass `-s <shell>` to
   `usermod`.
4. WHEN the coordinator specifies a new home directory THE SYSTEM SHALL pass `-d <home>` to
   `usermod`; when `move_home=True` THE SYSTEM SHALL also pass `-m`.
5. WHEN the coordinator specifies a new primary group THE SYSTEM SHALL pass `-g <group>` to
   `usermod`.
6. WHEN the coordinator specifies a new comment THE SYSTEM SHALL pass `-c "<comment>"` to
   `usermod`.
7. WHEN the coordinator specifies a new UID THE SYSTEM SHALL pass `-u <uid>` to `usermod`.
8. WHEN the `usermod` command succeeds THE SYSTEM SHALL return a `UserActionResult` with
   status `SUCCESS`.
9. WHEN the `usermod` command fails THE SYSTEM SHALL return a `UserActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
10. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
    `UserActionResult` with status `DRY_RUN`.

---

### Requirement 4: Query a Local User

**User Story:** As a maintenance coordinator, I want to query the properties of a local
user account, so that I can make conditional decisions about what modifications are needed
without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator queries a username THE SYSTEM SHALL read the user's entry from
   `/etc/passwd` via the command runner.
2. WHEN the user exists THE SYSTEM SHALL return a `UserInfo` record containing username,
   UID, GID, comment, home directory, and shell.
3. WHEN the user does not exist THE SYSTEM SHALL return `None`.
4. WHEN the read operation fails for an unexpected reason THE SYSTEM SHALL raise a
   `UserQueryError` with the captured error detail.

---

### Requirement 5: Create a Local Group

**User Story:** As a maintenance coordinator, I want to create a local group on the system,
so that the required group is present for file permissions and service account configuration.

#### Acceptance Criteria

1. WHEN the coordinator provides a group name THE SYSTEM SHALL invoke `groupadd <groupname>`
   via the command runner.
2. WHEN the coordinator specifies a GID THE SYSTEM SHALL pass `-g <gid>` to `groupadd`.
3. WHEN the coordinator sets `system_group=True` THE SYSTEM SHALL pass `-r` to `groupadd`.
4. WHEN the group already exists THE SYSTEM SHALL skip execution and return a
   `GroupActionResult` with status `SKIPPED`.
5. WHEN the `groupadd` command succeeds THE SYSTEM SHALL return a `GroupActionResult` with
   status `SUCCESS`.
6. WHEN the `groupadd` command fails THE SYSTEM SHALL return a `GroupActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `GroupActionResult` with status `DRY_RUN`.

---

### Requirement 6: Delete a Local Group

**User Story:** As a maintenance coordinator, I want to delete a local group from the
system, so that unused groups are removed cleanly.

#### Acceptance Criteria

1. WHEN the coordinator requests deletion of a group THE SYSTEM SHALL invoke
   `groupdel <groupname>` via the command runner.
2. WHEN the group does not exist THE SYSTEM SHALL skip execution and return a
   `GroupActionResult` with status `SKIPPED`.
3. WHEN the `groupdel` command succeeds THE SYSTEM SHALL return a `GroupActionResult` with
   status `SUCCESS`.
4. WHEN the `groupdel` command fails THE SYSTEM SHALL return a `GroupActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `GroupActionResult` with status `DRY_RUN`.

---

### Requirement 7: Add a User to a Group

**User Story:** As a maintenance coordinator, I want to add a user to a supplementary group,
so that the user gains the permissions and capabilities associated with that group.

#### Acceptance Criteria

1. WHEN the coordinator provides a username and group name THE SYSTEM SHALL invoke
   `usermod -aG <groupname> <username>` via the command runner.
2. WHEN the user is already a member of the group THE SYSTEM SHALL skip execution and
   return a `UserActionResult` with status `SKIPPED`.
3. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
4. WHEN the group does not exist THE SYSTEM SHALL raise a `GroupNotFoundError`.
5. WHEN the `usermod` command succeeds THE SYSTEM SHALL return a `UserActionResult` with
   status `SUCCESS`.
6. WHEN the `usermod` command fails THE SYSTEM SHALL return a `UserActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `UserActionResult` with status `DRY_RUN`.

---

### Requirement 8: Remove a User from a Group

**User Story:** As a maintenance coordinator, I want to remove a user from a supplementary
group, so that the user's group-based permissions are revoked cleanly.

#### Acceptance Criteria

1. WHEN the coordinator provides a username and group name THE SYSTEM SHALL invoke
   `gpasswd -d <username> <groupname>` via the command runner.
2. WHEN the user is not a member of the group THE SYSTEM SHALL skip execution and return a
   `UserActionResult` with status `SKIPPED`.
3. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
4. WHEN the group does not exist THE SYSTEM SHALL raise a `GroupNotFoundError`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `UserActionResult` with status
   `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UserActionResult` with status `FAILED`,
   the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `UserActionResult` with status `DRY_RUN`.

---

### Requirement 9: Manage SSH Authorized Keys

**User Story:** As a maintenance coordinator, I want to add or remove an SSH public key in
a user's `authorized_keys` file, so that key-based SSH access for that user can be
provisioned or deprovisioned without modifying the full key file.

#### Acceptance Criteria

1. WHEN the coordinator provides a username and a public key string to add THE SYSTEM SHALL
   append the key to `~<username>/.ssh/authorized_keys` if it is not already present.
2. WHEN the key is already present in the file THE SYSTEM SHALL skip the write and return a
   `UserActionResult` with status `SKIPPED`.
3. WHEN the `.ssh` directory does not exist THE SYSTEM SHALL create it with mode `0700`
   and the correct ownership before writing the key.
4. WHEN the `authorized_keys` file does not exist THE SYSTEM SHALL create it with mode
   `0600` and the correct ownership before writing the key.
5. WHEN the coordinator provides a public key string to remove THE SYSTEM SHALL remove the
   matching line from `authorized_keys` and return `SKIPPED` if the key is not present.
6. WHEN the coordinator provides a username and no key is specified THE SYSTEM SHALL raise
   a `UserKeyError` indicating a key must be provided.
7. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT write or modify any file and SHALL
   return a `UserActionResult` with status `DRY_RUN`.

---

### Requirement 10: Lock and Unlock a User Account

**User Story:** As a maintenance coordinator, I want to lock or unlock a local user account,
so that a user's login access can be suspended or restored without deleting the account.

#### Acceptance Criteria

1. WHEN the coordinator requests locking a user account THE SYSTEM SHALL invoke
   `usermod -L <username>` via the command runner.
2. WHEN the account is already locked THE SYSTEM SHALL skip execution and return a
   `UserActionResult` with status `SKIPPED`.
3. WHEN the coordinator requests unlocking a user account THE SYSTEM SHALL invoke
   `usermod -U <username>` via the command runner.
4. WHEN the account is already unlocked THE SYSTEM SHALL skip execution and return a
   `UserActionResult` with status `SKIPPED`.
5. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
6. WHEN the lock or unlock command succeeds THE SYSTEM SHALL return a `UserActionResult`
   with status `SUCCESS`.
7. WHEN the lock or unlock command fails THE SYSTEM SHALL return a `UserActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `UserActionResult` with status `DRY_RUN`.

---

### Requirement 11: Set and Expire Passwords

**User Story:** As a maintenance coordinator, I want to set a password expiry policy on a
user account or force an immediate password expiry, so that password rotation requirements
are enforced on the system.

#### Acceptance Criteria

1. WHEN the coordinator requests immediate password expiry for a user THE SYSTEM SHALL
   invoke `passwd --expire <username>` via the command runner.
2. WHEN the coordinator provides a maximum password age in days THE SYSTEM SHALL invoke
   `chage -M <days> <username>` via the command runner.
3. WHEN the coordinator provides a minimum password age in days THE SYSTEM SHALL invoke
   `chage -m <days> <username>` via the command runner.
4. WHEN the coordinator provides an account expiry date THE SYSTEM SHALL invoke
   `chage -E <YYYY-MM-DD> <username>` via the command runner.
5. WHEN the user does not exist THE SYSTEM SHALL raise a `UserNotFoundError`.
6. WHEN the command succeeds THE SYSTEM SHALL return a `UserActionResult` with status
   `SUCCESS`.
7. WHEN the command fails THE SYSTEM SHALL return a `UserActionResult` with status `FAILED`,
   the exit code, and the captured stderr.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return a
   `UserActionResult` with status `DRY_RUN`.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all user and group write commands to
run with the privileges required by `useradd`, `usermod`, `groupadd`, and related tools,
so that account management succeeds on a standard system where the process does not run
as root.

#### Acceptance Criteria

1. WHEN the system is configured to use sudo elevation THE SYSTEM SHALL pass `elevated=True`
   to the command runner for all write operations (create/delete/modify user, create/delete
   group, add/remove group member, manage SSH keys, lock/unlock, expire password).
2. WHEN performing read-only operations (query user, read authorized_keys) THE SYSTEM SHALL
   NOT use elevation.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation and
   allow the command runner to surface the resulting permission error.
