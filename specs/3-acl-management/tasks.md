# Tasks

## Source Module

- [x] Create `aclmanagement.py` under
      `source/packages/ccai/sysman/windows/`.
- [x] Module docstring covering the verb catalogue, file-I/O
      policy, elevation policy, idempotency policy, ACL-model
      commentary, and the canonical PowerShell argv shape.
- [x] Module-level binary / flag constants (`_DEFAULT_POWERSHELL`,
      `_PS_NOPROFILE`, `_PS_NONINTERACTIVE`, `_PS_COMMAND`,
      `_ICACLS`, `_ICACLS_RESET`, `_ICACLS_GRANT_REPLACE`,
      `_ICACLS_DENY`, `_ICACLS_REMOVE`).
- [x] Alias `WindowsAclActionStatus` to the package-shared
      `ActionStatus`.
- [x] `WindowsAcePermission` enum with `ALLOW`, `DENY`.
- [x] `WindowsAceInheritance` enum with `NONE`, `OBJECT_INHERIT`,
      `CONTAINER_INHERIT`, `BOTH`.
- [x] `WindowsAclManagerConfig` dataclass extending
      `FeatureConfig` with `powershell_executable` and
      `icacls_executable` fields.
- [x] `WindowsAclActionResult` dataclass extending
      `FeatureActionResult` with `path`, `principal`, `message`
      fields.
- [x] `WindowsAceInfo` dataclass for parsed read-shape ACEs (with
      `principal`, `permission`, `rights`, `inheritance`,
      `is_inherited`).
- [x] `WindowsAceSpec` dataclass for caller-supplied write-shape
      ACEs.
- [x] `WindowsAclInfo` dataclass for the per-path snapshot.
      `default_aces` is always empty on Windows; `sddl` carries
      the canonical NTFS round-trip serialisation.
- [x] `WindowsAclQueryError` exception with `command`, `exit_code`,
      `stderr` fields and a multi-line message body.

## Manager Class

- [x] `WindowsAclManager(BaseFeatureManager)` with class variables
      `result_class`, `status_class`, `feature_name`.
- [x] `get_acl(path)` -> `WindowsAclInfo`.  Dispatches the
      `Get-Acl | ConvertTo-Json` PowerShell scriptblock; raises
      `WindowsAclQueryError` on non-zero.
- [x] `list_aces(path)` -> `list[WindowsAceInfo]`.  Re-uses
      `get_acl`; emits parsed ACEs in source order.
- [x] `set_acl(path, acl_spec)` -> `WindowsAclActionResult`.
      Validates rights tokens, dispatches `icacls /reset` then
      `icacls /grant:r` (or `/deny`) per ACE.  Skipped when the
      parsed current ACL matches.  Dry-run short-circuits.
- [x] `add_ace(path, principal, rights=None)` ->
      `WindowsAclActionResult`.  Validates rights tokens, dispatches
      `icacls /grant:r` or `icacls /deny`.  Skipped when the ACE is
      already present.  Dry-run short-circuits.
- [x] `remove_ace(path, principal)` -> `WindowsAclActionResult`.
      Dispatches `icacls /remove '<principal>'`.  Skipped when no
      explicit ACE matches the principal.  Dry-run short-circuits.
- [x] `clear_acl(path)` -> `WindowsAclActionResult`.  Dispatches
      `icacls /reset`.  Skipped when no explicit ACEs exist.
      Dry-run short-circuits.
- [x] `copy_acl(src, dst)` -> `WindowsAclActionResult`.  Dispatches
      a single PowerShell `Get-Acl '<src>' | Set-Acl '<dst>'`
      pipeline.  Skipped when source and destination SDDL match.
      Dry-run short-circuits.

## Module-Level Helpers

- [x] `_RIGHTS_LETTER_MAP` short-letter alias map.
- [x] `_KNOWN_RIGHTS_TOKENS` canonical `[FileSystemRights]`
      vocabulary.
- [x] `_ps_escape_single_quoted(value)` for embedding paths /
      principals inside PowerShell single-quoted strings.
- [x] `_materialise_ace_spec(principal, rights_override)` for
      caller convenience on `add_ace`.
- [x] `_find_unknown_rights_tokens(rights)` for token validation.
- [x] `_render_icacls_rights_token(rights)` rendering the
      parenthesised rights body.
- [x] `_render_icacls_inheritance_prefix(inheritance)` rendering
      the `(OI)` / `(CI)` / `(OI)(CI)` prefix.
- [x] `_render_icacls_ace_arguments(spec)` rendering the full
      icacls argv tail (verb + principal:rights token).
- [x] `_parse_get_acl_payload(output, path)` -> `WindowsAclInfo`.
- [x] `_normalize_access_records(raw)` for the bare-object /
      array shape duality.
- [x] `_parse_ace_record(record)` for a single Access record.
- [x] `_coerce_inheritance(raw)` for integer / string / flag-list
      `InheritanceFlags` encodings.
- [x] `_ace_already_present(current, spec)` for `add_ace`
      idempotency.
- [x] `_ace_principal_present(current, principal)` for
      `remove_ace` idempotency.
- [x] `_has_explicit_aces(info)` for `clear_acl` idempotency.
- [x] `_acl_matches_spec(current, desired)` for `set_acl`
      idempotency.
- [x] `_ace_sort_key(item)` ordering helper.
- [x] `_acl_infos_equal(left, right)` for `copy_acl` idempotency.

## Test Module

- [x] Create `test_aclmanagement.py` under
      `source/testroots/testbase/ccai/tests/sysman/windows/`.
- [x] `_MockCommandRunner` recorder.
- [x] `_full_acl_payload()` JSON fixture (multi-ACE array form;
      one allow user, one allow group, one deny, one inherited).
- [x] `_single_ace_payload()` JSON fixture (single-ACE bare-
      object form — the `ConvertTo-Json` quirk).
- [x] `_minimal_acl_payload()` JSON fixture (no explicit ACEs).
- [x] `_seed_full_acl(runner, path)` and friends.
- [x] `test_get_acl_parses_powershell_json`.
- [x] `test_get_acl_parses_single_ace_object_form`.
- [x] `test_set_acl_invokes_set_acl`.
- [x] `test_set_acl_skipped_when_match`.
- [x] `test_set_acl_dry_run_short_circuits`.
- [x] `test_add_ace_invokes_powershell`.
- [x] `test_add_ace_skipped_when_present`.
- [x] `test_add_ace_with_inheritance_flags`.
- [x] `test_remove_ace_invokes_icacls`.
- [x] `test_remove_ace_skipped_when_absent`.
- [x] `test_clear_acl_invokes_icacls_reset`.
- [x] `test_copy_acl_invokes_pipeline`.
- [x] `test_copy_acl_skipped_when_match`.
- [x] `test_list_aces_returns_full_set`.
- [x] `test_writes_elevate`.
- [x] `test_reads_never_elevate`.
- [x] `test_failed_command_surfaces_stderr`.
- [x] `test_unknown_rights_token_returns_failed`.

## Specs

- [x] `requirements.md` covering 11 numbered requirements (plus a
      twelfth on error reporting / no-direct-FS-access).
- [x] `design.md` covering the cross-OS ACL-model divergence,
      verb-to-command mapping, parsing, idempotency, dry-run,
      elevation, error surface, aspect composition, ACL-model
      concept mapping, and spec-vs-contract drift.
- [x] `tasks.md` (this document).

## Gaps (deferred)

- **SDDL editing.** The manager exposes the parsed `Sddl` string on
  `WindowsAclInfo` for byte-equality comparison in `copy_acl`, but
  it does NOT accept a caller-supplied SDDL string as a `set_acl`
  input.  Direct SDDL writes go through `Set-Acl` with a manually
  constructed `[RawSecurityDescriptor]`; that surface is deferred
  to a future `set_sddl` verb when a caller actually needs it.
- **Ownership change as a separate verb.** `icacls /setowner` is
  already exposed by `3-filesystem-management` as
  `WindowsFilesystemManager.chown`; reproducing it here would
  duplicate the surface.  A future `set_owner` verb on this
  manager would only make sense if it grew NTFS-specific options
  (e.g., taking a caller-supplied SID rather than a name) that the
  filesystem-manager surface does not need.
- **Audit (SACL) ACEs.** `Get-Acl -Audit` returns the System Access
  Control List which governs auditing.  This pilot exposes only
  the DACL.  A future `get_sacl` / `set_sacl` pair will live in a
  follow-up `3-audit-management` extension.
- **Recursive operations.** The cross-OS contract on Windows allows
  `recursive=True` via `icacls /T` and `Set-Acl -Path <enumerated>`,
  but recursive ACL walks demand a richer reporting shape (per-
  child status) than the current per-path verbs provide.  Reserved
  for a follow-up `3-acl-management-recursive` spec.

## Follow-ups

- **Cross-OS contract conformance review.**  Once the cross-OS
  adapter layer in `codecraftai-interfaces` materialises, revisit the
  type-name divergence (`WindowsAclInfo` vs the contract's
  `AclSet`) and decide whether the adapter or the per-OS module
  carries the rename.
- **Principal-format cross-OS normalization.**  The contract's
  `AclPrincipal` carries a `kind` / `name` / `sid` triple; this
  spec carries the principal as a single `str` (`DOMAIN\name` /
  `BUILTIN\<group>` / `Everyone`).  When the adapter layer needs
  to normalise across Linux user/group separation and Windows SID-
  resolved names, decide whether to expose a Windows-side
  `WindowsAclPrincipal` dataclass or to do the translation at the
  adapter boundary.
- **Rights-token vocabulary expansion.**  The current
  `_KNOWN_RIGHTS_TOKENS` set covers the canonical
  `[FileSystemRights]` enum surface.  Tokens emitted by
  `Get-Acl` for unusual ACEs (e.g., generic-rights `4` /
  `0x40000000`) currently fall through verbatim on the parse path
  and are rejected on the write path.  Decide whether to grow the
  vocabulary or to add a dedicated `WindowsAclRights` enum that
  pins the supported subset.

## Out of Scope

- Active Directory account resolution edge cases (e.g., trusted-
  domain SID translation) — `icacls` and `Get-Acl` handle
  resolution; this feature does not duplicate it.
- Windows registry ACLs (`Get-Acl 'HKLM:\Software\...'`) — out of
  scope for filesystem ACLs.  A future `3-registry-management`
  spec may expose them.
- Share-level ACLs (`Get-SmbShareAccess`) — out of scope.  Covered
  by the SMB share-management feature when that lands.
- Mandatory Integrity Levels (the SDDL `S:(ML;...)` partition) —
  out of scope; addressable through a future `set_sddl` verb.
