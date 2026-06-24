# Backup / Export Contract（备份与导出契约）

## 0. 文档目的

本文定义管理员完整备份、普通用户授权范围导出、SQLite 在线备份、manifest、restore 和迁移导出规则。

---

## 1. Backup Types

```text
admin_full_backup
user_authorized_export
migration_export
```

### 1.1 admin_full_backup

Requires admin/runtime backup capability.

May include:

```text
SQLite database
videos/
frames/
artifacts/
config snapshot without secrets
manifest
checksums
analysis_timeline_events (diagnostic, sanitized)
```

### 1.2 user_authorized_export

Requires current principal and AuthorizedScope.

Must include only authorized resources:

```text
authorized VideoSource metadata
authorized ObservationRecord data
authorized locator/exported clips if requested and allowed
audit/export manifest
```

Must not include full SQLite DB file.

Timeline observability events are diagnostic and may include detailed operational
metadata. They MUST be excluded from `user_authorized_export` unless a future
explicit user-facing diagnostic export contract is added with authorization and
redaction review.

### 1.3 migration_export

For SQLite -> PostgreSQL or version migration.

Uses contract schema rows, not ORM private objects.

Migration export may include `analysis_timeline_events` as optional diagnostic
rows because they are sanitized contract DTOs; importers may also omit them
without changing business state.

---

## 2. Manifest Shape

```json
{
  "schema_version": "v1",
  "app_version": "0.1.0",
  "backup_type": "admin_full_backup",
  "created_at": "...",
  "created_by_principal_id": "admin_001",
  "database_engine": "sqlite",
  "data_scope": "admin_full",
  "included_paths": [],
  "table_counts": {},
  "checksum": {
    "algorithm": "sha256",
    "value": "..."
  }
}
```

---

## 3. SQLite Online Backup

Running SQLite backup must use one of:

```text
SQLite backup API
controlled checkpoint + copy procedure
service quiesce + WAL checkpoint + copy
```

Forbidden:

```text
copy active sqlite file while writes continue and claim consistency
```

---

## 4. User Export Authorization

User export flow:

```text
authenticate principal
check export capability
compute AuthorizedScope
query only authorized records
package sanitized data/artifacts
append audit event
```

Forbidden:

```text
export full DB file for normal user
include forbidden record in counts/facets/metadata
include internal source_uri
include credentials/secrets
```

---

## 5. Restore Rules

Admin restore must validate:

```text
manifest schema_version supported
checksum valid
backup_type allowed
schema migration path exists
storage paths safe
```

Restore must not overwrite existing production data without explicit operator confirmation.

---

## 6. Video / Frame / Artifact Inclusion

Defaults:

```text
admin_full_backup: include database; media inclusion configurable
user_export: include metadata by default; clips/thumbnails optional and authorized
migration_export: database rows only unless media migration requested
```

Frames are usually reconstructable and may be omitted.

---

## 7. Audit Requirements

Audit events:

```text
backup_started
backup_succeeded
backup_failed
export_started
export_succeeded
export_failed
restore_started
restore_succeeded
restore_failed
```

Audit must include:

```text
principal_id
backup_type/export_type
scope_hash if user export
manifest checksum
created_at
```

---

## 8. Contract Tests

```text
admin_backup_manifest_valid
user_export_excludes_forbidden_records
user_export_does_not_include_sqlite_file
sqlite_online_backup_consistent
restore_rejects_bad_checksum
restore_rejects_unsupported_schema
backup_audit_event_written
```
