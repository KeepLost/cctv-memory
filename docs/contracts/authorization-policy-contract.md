# Authorization Policy Contract（权限策略契约）

## 0. 文档目的

本文定义 CCTV Memory 的权限策略数据格式、授权计算规则、资源继承规则和错误语义。

原则：

- 身份来自服务端验证后的 token/session/mTLS，不来自 request body；
- 权限策略持久化在数据库；
- 用户可见查询必须先计算 AuthorizedScope；
- 无权资源对合法查询表现为不存在；
- capability denied 与 resource hidden 是不同语义。

---

## 1. Principal

```json
{
  "principal_id": "user_001",
  "principal_type": "user",
  "tenant_id": "tenant_default",
  "external_subject_id": null,
  "display_name": "Security User",
  "status": "active",
  "roles": ["security_viewer"],
  "groups": ["security_team"]
}
```

规则：

- `status != active` 的 principal 不得访问业务 API；
- service_account 可以用于外部 recorder、批处理和内部系统；
- admin 不自动绕过审计。

---

## 2. Capability

第一版 capability：

```text
observation.search
observation.read_detail
observation.read_locator
video.playback
analysis.submit
analysis.rerun
analysis.publish
camera.manage
policy.manage
user.manage
audit.read
runtime.manage
```

AI-facing 默认 capability：

```text
observation.search
observation.read_detail
observation.read_locator
video.playback
```

接口能力非法时返回：

```text
403 capability_denied
```

---

## 3. AccessPolicy

```json
{
  "access_policy_id": "policy_lab_confidential",
  "tenant_id": "tenant_default",
  "name": "研发实验室机密策略",
  "security_level": "confidential",
  "rules": {
    "allowed_roles": ["security_admin", "lab_manager"],
    "allowed_groups": ["lab_team"],
    "allowed_principals": [],
    "denied_principals": []
  },
  "created_at": "...",
  "updated_at": "..."
}
```

规则：

- `denied_principals` 优先于 allowed；
- role/group/principal 任一满足 allowed 即可访问；
- `security_level` 同时用于粗粒度过滤和审计；
- 第一版不做复杂表达式语言，避免策略引擎过度设计。

---

## 4. AuthorizedScope

```json
{
  "tenant_id": "tenant_default",
  "principal_id": "user_001",
  "allowed_camera_ids": ["cam_lobby_01"],
  "allowed_location_ids": ["loc_lobby_01"],
  "allowed_access_policy_ids": ["policy_public_area"],
  "max_security_level": "internal",
  "capabilities": ["observation.search", "observation.read_detail"],
  "scope_hash": "scope_hash_abc"
}
```

计算输入：

```text
principal
roles
groups
access_policies
camera_locations
camera_devices
```

计算输出必须稳定，可生成 `scope_hash` 绑定 SearchContext。

### 4.1 AuthorizedScope 过滤组合语义

用户可见资源查询必须按以下规则解释 AuthorizedScope：

```text
capability check
AND tenant_id match
AND camera_id IN allowed_camera_ids
AND location_id IN allowed_location_ids
AND access_policy_id IN allowed_access_policy_ids
AND security_level <= max_security_level
```

规则：

- 维度之间默认使用 **AND**，不是 OR；
- `allowed_camera_ids` / `allowed_location_ids` / `allowed_access_policy_ids` 的空数组表示 **该维度无许可**，不得解释为无限制；
- 如果系统确实需要表达“不按某一资源维度限制”，必须使用显式的 admin/service bypass 机制或专门 schema 字段，不能用空数组表达；
- `max_security_level` 必须存在；缺失或无法比较时按拒绝访问处理；
- `capabilities` 只决定是否允许调用某类接口，不替代资源范围过滤；
- SearchContext 保存的 `scope_hash` 必须由上述所有资源维度和 capability 摘要共同计算。

安全默认值：任何无法解析、无法比较、字段缺失或维度为空导致的歧义，都应 fail closed（拒绝/空结果），而不是 fail open。

---

## 5. 资源权限继承

继承链：

```text
CameraLocation.access_policy_id / security_level
  ↓
CameraDevice.access_policy_id（为空则继承 location）
  ↓
VideoSource.access_policy_id（为空则继承 camera/location）
  ↓
ObservationRecord.access_policy_id / security_level（写入时快照）
```

规则：

- ObservationRecord 权限由系统派生，不由 VLM 输出；
- 如果上游资源策略为空，使用系统默认 policy；
- 如果多层策略冲突，默认取更严格策略；
- 权限变更后，历史 ObservationRecord 是否重算由 admin maintenance 决定，必须审计。

### 5.1 security_level 顺序与“更严格”定义

全局固定 security level 顺序：

```text
public < internal < confidential < restricted
```

比较规则：

- `security_level <= max_security_level` 才可访问；
- 资源继承链上多层 security_level 冲突时，取顺序中更高的一项；
- 多层 `access_policy_id` 冲突时，默认取与更高 security_level 绑定的策略；如果无法判断，取更窄 allowed 集合或 fail closed；
- admin maintenance 可以显式重算 ObservationRecord 权限快照，但必须审计。

---

## 6. 查询授权规则

### 6.1 合法接口 + 无权资源

合法查询命中无权资源时：

```text
结果中不出现
facet/count/top_tags 不统计
record_id details 返回 not_found 或空
locator 不返回
```

默认回答建议：

```text
没有找到你可访问范围内的匹配记录。
```

### 6.2 非法接口

没有 capability 时返回明确错误：

```json
{
  "code": "capability_denied",
  "message": "This operation is not permitted for the current principal."
}
```

---

## 7. SearchContext 授权绑定

创建 SearchContext 时保存：

```text
principal_id
tenant_id
session_id
authorized_scope_hash
dataset_revision
```

后续调用必须：

1. 重新验证 token；
2. 重新计算或校验 AuthorizedScope；
3. 不允许 refine 扩大到当前 principal 无权范围；
4. context_id 不作为权限凭证。

---

## 8. Locator / Playback 授权

locator 和 playback 必须二次鉴权。

流程：

```text
record_id/playback_token
  ↓
查记录与 VideoSource
  ↓
校验当前 principal 是否仍可访问
  ↓
返回短 TTL URL 或代理播放
```

playback token 建议绑定：

```text
principal_id
record_id/video_id
segment_start_ms
segment_end_ms
expires_at
```

---

## 9. 导出与备份授权

```text
admin_full_backup -> 需要 admin/runtime 或 backup capability，可包含完整 DB
user_export -> 必须按 AuthorizedScope 过滤，仅导出授权范围
```

普通用户不得直接下载完整 SQLite 数据库文件。

---

## 10. 审计要求

必须审计：

```text
login/logout/refresh
query/facet/details/locator/playback
analysis.submit/rerun/publish
policy/user/camera changes
backup/export/restore
capability_denied
```

审计事件不得记录明文 token 或敏感密钥。

---

## 11. 策略变更规则

策略变更必须记录：

```text
old_policy
new_policy
changed_by
changed_at
reason
```

变更后：

- 新查询使用新策略；
- 已存在 SearchContext 可选择立即失效或下次 refine 时重新校验；
- ObservationRecord 权限快照是否重算必须作为 admin maintenance 操作处理。

MVP 推荐：策略变更后使相关 active SearchContext 失效，避免权限漂移。
