# Object Detection Schema Design

## 0. Purpose

This document designs the two-layer object detection schema for CCTV Memory:

- Common layer: vendor-neutral DTOs used by CCTV Memory workers, application services, storage, search, and UI overlay logic.
- Adapter layer: vendor-specific request/response DTOs and mapping rules. Each provider owns one adapter that converts common request to provider request and provider response to common result.

This is a design document only. It does not add a public API endpoint, implement a provider, or change existing business code.

## 1. Current Code Compatibility Review

The existing project already has the right architecture seams:

- `cctv_memory/contracts/` contains explicit Pydantic schemas for cross-module DTOs.
- `cctv_memory/services/` contains abstract service ports such as `VlmAnalyzerPort`, `MotionDetectorPort`, and current `DetectorGatePort`.
- `cctv_memory/infrastructure/` contains concrete adapters, including VLM and video implementations.
- `cctv_memory/workers/default_segment.py` runs the current detector gate before optional VLM calls.
- `DetectorGateLog` stores detector evidence separately from natural-language records.
- Detector-only records use empty `static_description_text`, empty `dynamic_description_text`, empty `tags`, and put detector evidence under `ObservationRecord.attributes.detector_gate`.

The proposed object detection layer should therefore be introduced as:

- common DTOs under a future `cctv_memory/contracts/object_detection.py`;
- a service port under a future `cctv_memory/services/object_detection.py`;
- provider adapters under a future `cctv_memory/infrastructure/object_detection/` or equivalent infrastructure subpackage;
- optional worker integration through the existing detector gate path, not through API routers or domain imports of vendor SDKs.

This keeps the architecture constitution intact: API/workers call application/service ports, contracts define explicit shapes, and infrastructure owns vendor-specific details.

## 2. Design Goals

- Business code never depends on Google Vision, or any other vendor's raw JSON.
- Common detections preserve enough geometry for storage, retrieval evidence, and UI overlays.
- Coordinates are stable across image resizing and frontend rendering.
- Batch requests are first-class because Google Vision and future providers can process multiple images per call.
- Per-image failures are representable without failing the entire batch by default.
- Raw media bytes/base64, absolute frame paths, credentials, and source URIs are not persisted in business records.
- Detector labels are evidence metadata by default; they are not automatically written into `tags` or natural-language descriptions.

## 3. Common Layer TypeScript Model

The common layer is the only schema layer visible to workers/application code.

```ts
export type ObjectDetectionSchemaVersion = "object_detection_v1";

export type ObjectDetectionProvider =
  | "google_vision"
  | "mock"
  | string;

export type ImageInputKind = "bytes_base64" | "uri" | "artifact_ref";

export interface ObjectDetectionImageInput {
  image_id: string;
  kind: ImageInputKind;
  content_base64?: string;
  uri?: string;
  artifact_ref?: string;
  mime_type?: "image/jpeg" | "image/png" | "image/webp" | string;
  width_px?: number;
  height_px?: number;
  frame_index?: number;
  timestamp_ms?: number;
  sha256?: string;
}

export interface ObjectDetectionRequestOptions {
  max_results?: number;
  min_confidence?: number;
  label_allowlist?: string[];
  provider_options?: Record<string, unknown>;
}

export interface ObjectDetectionBatchRequest {
  schema_version: ObjectDetectionSchemaVersion;
  request_id: string;
  provider: ObjectDetectionProvider;
  images: ObjectDetectionImageInput[];
  options?: ObjectDetectionRequestOptions;
}
```

Rules:

- `image_id` is caller-generated and must be echoed in each response item.
- `content_base64` is allowed only as transient request input. It must not be stored in `ObservationRecord`, `DetectorGateLog`, model call logs, or timeline events.
- `artifact_ref` is preferred inside CCTV Memory because it can refer to already materialized selected frames without exposing absolute paths.
- `width_px` and `height_px` should be included when known. If the provider returns only normalized coordinates, image size remains optional but useful for display and debugging.
- `provider_options` is an escape hatch for adapter-owned knobs and must not override core common fields.

## 4. Detection Result Model

```ts
export type BoundingBoxCoordinateSpace = "normalized" | "pixel";
export type BoundingBoxFormat = "xywh" | "xyxy" | "polygon";

export interface Point2D {
  x: number;
  y: number;
}

export interface BoundingBox {
  coordinate_space: BoundingBoxCoordinateSpace;
  format: BoundingBoxFormat;
  xywh?: { x: number; y: number; width: number; height: number };
  xyxy?: { x_min: number; y_min: number; x_max: number; y_max: number };
  polygon?: Point2D[];
}
```

```ts
export interface DetectionCategory {
  provider_category_id?: string;
  normalized_label?: string;
  display_name?: string;
  taxonomy?: "google_kg_mid" | "cctv_memory" | string;
}

export interface ObjectDetectionItem {
  detection_id: string;
  label: string;
  confidence: number;
  bbox: BoundingBox;
  category?: DetectionCategory;
  source_provider: ObjectDetectionProvider;
  provider_detection_id?: string;
  attributes?: Record<string, unknown>;
}

export interface ObjectDetectionImageResult {
  image_id: string;
  image: {
    width_px?: number;
    height_px?: number;
    frame_index?: number;
    timestamp_ms?: number;
    sha256?: string;
  };
  status: "succeeded" | "failed";
  detections: ObjectDetectionItem[];
  error?: ObjectDetectionError;
  provider_response_id?: string;
}

export interface ObjectDetectionBatchResult {
  schema_version: ObjectDetectionSchemaVersion;
  request_id: string;
  provider: ObjectDetectionProvider;
  model_id?: string;
  results: ObjectDetectionImageResult[];
  usage?: {
    image_count: number;
    provider_request_count?: number;
    latency_ms?: number;
  };
}
```

Rules:

- `confidence` is always normalized to `[0, 1]`.
- Default sorting is descending by `confidence`, with stable provider order as a tie-breaker.
- `label` is the provider-visible object name after minimal normalization. Do not force a global enum in v1.
- `category.provider_category_id` stores IDs such as Google `mid` (`/m/0h9mv`).
- `category.normalized_label` is optional and reserved for future project-owned vocabulary such as `person`, `vehicle`, or `bicycle`.
- `bbox.coordinate_space="normalized"` is the preferred canonical format for storage and overlay because it is independent of display size.
- For rectangular provider boxes, store both `polygon` and derived `xywh` when useful. The adapter must declare which one is canonical.

## 5. JSON Schema Sketch

The TypeScript model above can be represented as JSON Schema for contract tests. This sketch shows the important validation constraints, not a full generated schema.

```json
{
  "$id": "https://cctv-memory.local/schemas/object-detection-batch-result.v1.json",
  "type": "object",
  "required": ["schema_version", "request_id", "provider", "results"],
  "properties": {
    "schema_version": { "const": "object_detection_v1" },
    "request_id": { "type": "string", "minLength": 1 },
    "provider": { "type": "string", "minLength": 1 },
    "model_id": { "type": "string" },
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["image_id", "status", "detections"],
        "properties": {
          "image_id": { "type": "string", "minLength": 1 },
          "status": { "enum": ["succeeded", "failed"] },
          "detections": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["detection_id", "label", "confidence", "bbox", "source_provider"],
              "properties": {
                "detection_id": { "type": "string", "minLength": 1 },
                "label": { "type": "string", "minLength": 1 },
                "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
                "source_provider": { "type": "string", "minLength": 1 }
              }
            }
          }
        }
      }
    }
  }
}
```

## 6. Error Model

```ts
export type ObjectDetectionErrorCode =
  | "validation_error"
  | "payload_too_large"
  | "object_detection_provider_error"
  | "object_detection_rate_limited"
  | "object_detection_schema_validation_failed"
  | "dependency_unavailable"
  | "internal_error";

export interface ObjectDetectionError {
  code: ObjectDetectionErrorCode;
  message: string;
  retryable: boolean;
  provider_status?: string;
  provider_code?: string | number;
  details?: Record<string, unknown>;
}
```

Mapping guidance:

- Provider 4xx request validation problems map to `validation_error` unless they are auth/capability problems handled outside this adapter.
- Provider 429 maps to `object_detection_rate_limited` with `retryable=true`.
- Provider timeout, transport failure, and 5xx map to `object_detection_provider_error` or `dependency_unavailable` with `retryable=true`.
- Provider response shape mismatch maps to `object_detection_schema_validation_failed` with `retryable=false`.
- Messages must be bounded and redacted. Do not include tokens, absolute paths, raw base64, or provider authorization headers.

If implementation reuses the existing error-code contract directly, these object-detection-specific codes should either be added to that contract or internally mapped to existing `vlm_provider_error`, `vlm_rate_limited`, and `vlm_schema_validation_failed` equivalents. The design preference is to add explicit object-detection codes before shipping a public worker path.

## 7. Adapter Layer Interface

Each provider owns one adapter. The adapter boundary is the only place where provider request/response DTOs exist.

```ts
export interface ObjectDetectionProviderCapabilities {
  provider: ObjectDetectionProvider;
  supported_input_kinds: ImageInputKind[];
  supports_batch: boolean;
  max_images_per_batch?: number;
  supports_max_results: boolean;
  supports_min_confidence: boolean;
  bbox_coordinate_space: BoundingBoxCoordinateSpace;
  bbox_formats: BoundingBoxFormat[];
  returns_category_id: boolean;
}

export interface ObjectDetectionAdapter<TVendorRequest, TVendorResponse> {
  provider: ObjectDetectionProvider;
  capabilities(): ObjectDetectionProviderCapabilities;
  toVendorRequest(request: ObjectDetectionBatchRequest): TVendorRequest;
  fromVendorResponse(
    request: ObjectDetectionBatchRequest,
    response: TVendorResponse
  ): ObjectDetectionBatchResult;
  fromVendorError?(error: unknown): ObjectDetectionError;
}
```

Adapter rules:

- `toVendorRequest` validates common options against capabilities and fails early on unsupported core features.
- `fromVendorResponse` must always return one `ObjectDetectionImageResult` per input image, preserving order and `image_id`.
- Raw vendor response may be logged only in sanitized debug contexts. It must not become active business state.
- Provider auth, proxy URL, headers, and credentials are infrastructure configuration, not fields in the common request.
- Registration should be a simple provider-name registry, consistent with existing motion detector factory patterns. Configuration chooses `provider`; worker code depends on the `ObjectDetectionPort`, not on concrete adapters.

Future Python shape should mirror the existing service-port style:

```py
class ObjectDetectionPort(Protocol):
    def detect_objects(
        self,
        request: ObjectDetectionBatchRequest,
    ) -> ObjectDetectionBatchResult: ...
```

## 8. Google Vision Vendor Schema

The observed Google Vision Object Localization request is:

```json
{
  "requests": [
    {
      "image": { "content": "<BASE64_ENCODED_IMAGE>" },
      "features": [
        { "maxResults": 10, "type": "OBJECT_LOCALIZATION" }
      ]
    }
  ]
}
```

The observed response shape is:

```json
{
  "responses": [
    {
      "localizedObjectAnnotations": [
        {
          "mid": "/m/0h9mv",
          "name": "Tire",
          "score": 0.7344195,
          "boundingPoly": {
            "normalizedVertices": [
              { "x": 0.5390625, "y": 0.484375 },
              { "x": 0.83203125, "y": 0.484375 },
              { "x": 0.83203125, "y": 0.93359375 },
              { "x": 0.5390625, "y": 0.93359375 }
            ]
          }
        }
      ]
    }
  ]
}
```

Per-image Google errors are returned under `responses[].error`:

```json
{
  "responses": [
    {
      "error": {
        "code": 401,
        "message": "Request had invalid authentication credentials...",
        "status": "UNAUTHENTICATED"
      }
    }
  ]
}
```

## 9. Google Vision Mapping Example

Common request:

```json
{
  "schema_version": "object_detection_v1",
  "request_id": "od_req_001",
  "provider": "google_vision",
  "images": [
    {
      "image_id": "frame_001",
      "kind": "bytes_base64",
      "content_base64": "<BASE64_ENCODED_IMAGE>",
      "mime_type": "image/jpeg",
      "width_px": 640,
      "frame_index": 1,
      "timestamp_ms": 120500,
      "sha256": "sha256:..."
    }
  ],
  "options": { "max_results": 10 }
}
```

Adapter `toVendorRequest` output:

```json
{
  "requests": [
    {
      "image": { "content": "<BASE64_ENCODED_IMAGE>" },
      "features": [
        { "type": "OBJECT_LOCALIZATION", "maxResults": 10 }
      ]
    }
  ]
}
```

Field mapping:

| Common field | Google field | Rule |
|---|---|---|
| `images[].content_base64` | `requests[].image.content` | Only for transient request payload. |
| `images[].uri` | `requests[].image.source.imageUri` | Allowed only if policy permits URI inputs. |
| `options.max_results` | `features[].maxResults` | Omit if undefined. |
| provider feature type | `features[].type` | Always `OBJECT_LOCALIZATION`. |

Google response item for Tire maps to:

```json
{
  "detection_id": "frame_001:google_vision:0",
  "label": "Tire",
  "confidence": 0.7344195,
  "bbox": {
    "coordinate_space": "normalized",
    "format": "polygon",
    "polygon": [
      { "x": 0.5390625, "y": 0.484375 },
      { "x": 0.83203125, "y": 0.484375 },
      { "x": 0.83203125, "y": 0.93359375 },
      { "x": 0.5390625, "y": 0.93359375 }
    ],
    "xywh": {
      "x": 0.5390625,
      "y": 0.484375,
      "width": 0.29296875,
      "height": 0.44921875
    }
  },
  "category": {
    "provider_category_id": "/m/0h9mv",
    "display_name": "Tire",
    "taxonomy": "google_kg_mid"
  },
  "source_provider": "google_vision",
  "provider_detection_id": "/m/0h9mv",
  "attributes": {
    "google_mid": "/m/0h9mv"
  }
}
```

Batch result for the first three observed detections:

```json
{
  "schema_version": "object_detection_v1",
  "request_id": "od_req_001",
  "provider": "google_vision",
  "model_id": "google-vision-object-localization",
  "results": [
    {
      "image_id": "frame_001",
      "image": {
        "width_px": 640,
        "frame_index": 1,
        "timestamp_ms": 120500,
        "sha256": "sha256:..."
      },
      "status": "succeeded",
      "detections": [
        { "detection_id": "frame_001:google_vision:0", "label": "Tire", "confidence": 0.7344195, "source_provider": "google_vision", "bbox": { "coordinate_space": "normalized", "format": "polygon", "polygon": [{"x":0.5390625,"y":0.484375},{"x":0.83203125,"y":0.484375},{"x":0.83203125,"y":0.93359375},{"x":0.5390625,"y":0.93359375}] }, "category": { "provider_category_id": "/m/0h9mv", "display_name": "Tire", "taxonomy": "google_kg_mid" } },
        { "detection_id": "frame_001:google_vision:1", "label": "Tire", "confidence": 0.7274533, "source_provider": "google_vision", "bbox": { "coordinate_space": "normalized", "format": "polygon", "polygon": [{"x":0.12890625,"y":0.48632813},{"x":0.41210938,"y":0.48632813},{"x":0.41210938,"y":0.92578125},{"x":0.12890625,"y":0.92578125}] }, "category": { "provider_category_id": "/m/0h9mv", "display_name": "Tire", "taxonomy": "google_kg_mid" } },
        { "detection_id": "frame_001:google_vision:2", "label": "Bicycle", "confidence": 0.664858, "source_provider": "google_vision", "bbox": { "coordinate_space": "normalized", "format": "polygon", "polygon": [{"x":0.12695313,"y":0.26757813},{"x":0.8359375,"y":0.26757813},{"x":0.8359375,"y":0.9375},{"x":0.12695313,"y":0.9375}] }, "category": { "provider_category_id": "/m/0199g", "display_name": "Bicycle", "taxonomy": "google_kg_mid" } }
      ]
    }
  ],
  "usage": { "image_count": 1, "provider_request_count": 1 }
}
```

Google per-image error mapping:

| Google field | Common field |
|---|---|
| `responses[i].error.code` | `results[i].error.provider_code` |
| `responses[i].error.status` | `results[i].error.provider_status` |
| `responses[i].error.message` | `results[i].error.message`, redacted/bounded |
| `localizedObjectAnnotations` missing with error | `status="failed"`, `detections=[]` |

## 10. CCTV Memory Business Integration

The business flow should remain:

```text
CCTV video/chunk
  -> selected screenshots/frames
  -> object detection common request
  -> provider adapter
  -> common detection result
  -> detector gate decision / evidence log
  -> optional VLM enrichment
  -> PublicationService
  -> ObservationRecord + search/index/display
```

Storage guidance:

- `DetectorGateLog.frame_evidence` is the best current fit for per-frame detection evidence.
- `ObservationRecord.attributes.detector_gate` can store a compact summary such as `triggered_vlm`, matched rules, positive frame ratios, evidence hash, and gate log ID.
- Detailed per-object boxes should be stored in detector evidence/logs or bounded attributes only when needed for overlay/debugging.
- Raw provider JSON should not be stored as active record state. If retained for debugging, it must be sanitized, bounded, and governed by debug retention settings.
- `source_uri`, absolute frame paths, base64 image bytes, and credentials must never be stored in exported detector evidence.

Search and display guidance:

- Detection labels are useful evidence for candidate gating and UI overlays.
- Detection labels should not automatically become `ObservationRecord.tags`; current contracts explicitly say detector-only records keep `tags=[]`.
- Future promotion from detection labels to tags should be a separate publication policy with tests, because tags affect retrieval and user-visible semantics.
- Normalized polygons or `xywh` are appropriate for frontend overlay: display coordinate = normalized coordinate multiplied by rendered image size.
- `image.frame_index` and `image.timestamp_ms` connect a detection back to the source video segment without exposing internal paths.

## 11. Design Decisions

### 11.1 Use normalized coordinates as canonical storage

Google returns `normalizedVertices`, and normalized coordinates are independent of thumbnail or playback display size. Pixel coordinates may be derived when original dimensions are known, but normalized coordinates are the canonical v1 storage shape.

### 11.2 Preserve polygon and derive `xywh`

Google returns four vertices. Storing polygon preserves vendor fidelity and future rotated-box support. Deriving `xywh` is useful for simple overlay and matching logic. If only one form is implemented initially, choose normalized polygon and derive `xywh` at read/render time.

### 11.3 Do not define a closed category enum in v1

Object detection is open-world and vendor taxonomies differ. Common schema stores free-text `label`, optional provider category ID, and optional future normalized label. This avoids prematurely turning visual semantics into strong relational columns.

### 11.4 Batch response is per image, not all-or-nothing

Google `responses[]` maps one-to-one with `requests[]`, and one image can fail while others succeed. The common schema mirrors this with `ObjectDetectionImageResult.status` and per-image errors.

### 11.5 Adapter capabilities are explicit

Providers differ on batch size, `maxResults`, confidence threshold, URI support, and bbox formats. A capability method lets configuration/worker code fail early instead of silently sending unsupported options.

### 11.6 Provider metadata is evidence, not authority

Fields like Google `mid` are useful traceability metadata but should not drive access policy, security level, or durable business taxonomy without a separate reviewed mapping policy.

## 12. Security And Privacy Rules

- The adapter must not accept credentials in request DTOs.
- Proxy URL, token injection, and provider project headers are infrastructure configuration.
- Request/response logging must redact Authorization headers, tokens, base64 image content, and absolute paths.
- The common schema must not include `principal_id`, `access_policy_id`, or `security_level` as detector-provided fields.
- Any user-visible search/detail/playback path still uses existing AuthorizedScope and locator secondary authorization.

## 13. Compatibility Checklist

- Compatible with current `DetectorGatePort` direction: a future object detection port can replace or back the current mock detector gate without changing domain dependencies.
- Compatible with `DetectorGateLog`: common detections can be converted into frame evidence entries with label, confidence, bbox, timestamp, and hash metadata.
- Compatible with `ObservationRecord`: detector-only records can keep empty text/tags and store only compact detector summaries under attributes.
- Compatible with VLM flow: gate-positive windows can pass to existing VLM segment analysis unchanged.
- Compatible with API boundary: no public `/api/v1` route changes are required in this design round.
- Compatible with database migration posture: adding durable object-detection evidence fields later should be additive and migration-backed.

## 14. Open Implementation Notes For A Future Task

- Add Pydantic DTOs in `contracts/object_detection.py` matching this schema.
- Add `ObjectDetectionPort` under `services/object_detection.py`.
- Add `GoogleVisionObjectDetectionAdapter` under infrastructure, using the already tested proxy endpoint `http://nginx:7070/api/google/v1/images:annotate` via configuration.
- Add contract tests for Google request mapping, response mapping, per-image error mapping, bbox normalization, and redaction.
- Decide whether to replace `DetectorGatePort` DTOs with object-detection DTOs or adapt object-detection results into the existing gate DTOs. The minimal migration path is to adapt new common detections into current `DetectorFrameResult` until the gate path is refactored.
