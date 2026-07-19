"""Vendor-neutral object detection contracts.

The common layer is visible to workers/services; provider-specific JSON stays in
infrastructure adapters.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from cctv_memory.contracts.common import ContractModel

ObjectDetectionSchemaVersion = Literal["object_detection_v1"]
ObjectDetectionProvider = str
ImageInputKind = Literal["bytes_base64", "uri", "artifact_ref"]
BoundingBoxCoordinateSpace = Literal["normalized", "pixel"]
BoundingBoxFormat = Literal["xywh", "xyxy", "polygon"]
ObjectDetectionImageStatus = Literal["succeeded", "failed"]
ObjectDetectionErrorCode = Literal[
    "validation_error",
    "payload_too_large",
    "object_detection_provider_error",
    "object_detection_rate_limited",
    "object_detection_schema_validation_failed",
    "dependency_unavailable",
    "internal_error",
]


class ObjectDetectionImageInput(ContractModel):
    image_id: str = Field(min_length=1)
    kind: ImageInputKind
    content_base64: str | None = None
    uri: str | None = None
    artifact_ref: str | None = None
    mime_type: str | None = None
    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    frame_index: int | None = Field(default=None, ge=0)
    timestamp_ms: int | None = Field(default=None, ge=0)
    sha256: str | None = None

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> ObjectDetectionImageInput:
        if self.kind == "bytes_base64" and not self.content_base64:
            raise ValueError("bytes_base64 image input requires content_base64")
        if self.kind == "uri" and not self.uri:
            raise ValueError("uri image input requires uri")
        if self.kind == "artifact_ref" and not self.artifact_ref:
            raise ValueError("artifact_ref image input requires artifact_ref")
        return self


class ObjectDetectionRequestOptions(ContractModel):
    max_results: int | None = Field(default=None, gt=0)
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    label_allowlist: list[str] | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class ObjectDetectionBatchRequest(ContractModel):
    schema_version: ObjectDetectionSchemaVersion = "object_detection_v1"
    request_id: str = Field(min_length=1)
    provider: ObjectDetectionProvider = Field(min_length=1)
    images: list[ObjectDetectionImageInput] = Field(min_length=1)
    options: ObjectDetectionRequestOptions = Field(default_factory=ObjectDetectionRequestOptions)


class Point2D(ContractModel):
    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)


class BoundingBoxXywh(ContractModel):
    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)
    width: float = Field(ge=0.0)
    height: float = Field(ge=0.0)


class BoundingBoxXyxy(ContractModel):
    x_min: float = Field(ge=0.0)
    y_min: float = Field(ge=0.0)
    x_max: float = Field(ge=0.0)
    y_max: float = Field(ge=0.0)


class BoundingBox(ContractModel):
    coordinate_space: BoundingBoxCoordinateSpace = "normalized"
    format: BoundingBoxFormat
    xywh: BoundingBoxXywh | None = None
    xyxy: BoundingBoxXyxy | None = None
    polygon: list[Point2D] | None = None

    @model_validator(mode="after")
    def _validate_format_payload(self) -> BoundingBox:
        if self.format == "xywh" and self.xywh is None:
            raise ValueError("xywh bounding box requires xywh payload")
        if self.format == "xyxy" and self.xyxy is None:
            raise ValueError("xyxy bounding box requires xyxy payload")
        if self.format == "polygon" and not self.polygon:
            raise ValueError("polygon bounding box requires polygon payload")
        if self.coordinate_space == "normalized":
            for point in self.polygon or []:
                if point.x > 1.0 or point.y > 1.0:
                    raise ValueError("normalized polygon coordinates must be <= 1")
            if self.xywh is not None and (
                self.xywh.x > 1.0
                or self.xywh.y > 1.0
                or self.xywh.width > 1.0
                or self.xywh.height > 1.0
            ):
                raise ValueError("normalized xywh coordinates must be <= 1")
        return self


class DetectionCategory(ContractModel):
    provider_category_id: str | None = None
    normalized_label: str | None = None
    display_name: str | None = None
    taxonomy: str | None = None


class ObjectDetectionItem(ContractModel):
    detection_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox
    category: DetectionCategory | None = None
    source_provider: ObjectDetectionProvider = Field(min_length=1)
    provider_detection_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ObjectDetectionImageMetadata(ContractModel):
    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    frame_index: int | None = Field(default=None, ge=0)
    timestamp_ms: int | None = Field(default=None, ge=0)
    sha256: str | None = None


class ObjectDetectionError(ContractModel):
    code: ObjectDetectionErrorCode
    message: str
    retryable: bool = False
    provider_status: str | None = None
    provider_code: str | int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ObjectDetectionImageResult(ContractModel):
    image_id: str = Field(min_length=1)
    image: ObjectDetectionImageMetadata = Field(default_factory=ObjectDetectionImageMetadata)
    status: ObjectDetectionImageStatus = "succeeded"
    detections: list[ObjectDetectionItem] = Field(default_factory=list)
    error: ObjectDetectionError | None = None
    provider_response_id: str | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> ObjectDetectionImageResult:
        if self.status == "failed" and self.error is None:
            raise ValueError("failed image result requires error")
        return self


class ObjectDetectionUsage(ContractModel):
    image_count: int = Field(ge=0)
    provider_request_count: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class ObjectDetectionBatchResult(ContractModel):
    schema_version: ObjectDetectionSchemaVersion = "object_detection_v1"
    request_id: str = Field(min_length=1)
    provider: ObjectDetectionProvider = Field(min_length=1)
    model_id: str | None = None
    results: list[ObjectDetectionImageResult]
    usage: ObjectDetectionUsage | None = None


def polygon_to_xywh(points: list[Point2D]) -> BoundingBoxXywh:
    if not points:
        raise ValueError("polygon_to_xywh requires at least one point")
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return BoundingBoxXywh(
        x=min(xs),
        y=min(ys),
        width=max(xs) - min(xs),
        height=max(ys) - min(ys),
    )
