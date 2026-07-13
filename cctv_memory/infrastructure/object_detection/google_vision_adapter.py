"""Google Vision Object Localization adapter."""

from __future__ import annotations

from typing import Any

import httpx

from cctv_memory.contracts.object_detection import (
    BoundingBox,
    DetectionCategory,
    ObjectDetectionBatchRequest,
    ObjectDetectionBatchResult,
    ObjectDetectionError,
    ObjectDetectionImageMetadata,
    ObjectDetectionImageResult,
    ObjectDetectionItem,
    ObjectDetectionUsage,
    Point2D,
    polygon_to_xywh,
)
from cctv_memory.services.object_detection import ObjectDetectionPort


DEFAULT_GOOGLE_VISION_PROXY_URL = "http://nginx:7070/api/google/v1/images:annotate"


class GoogleVisionObjectDetectionAdapter(ObjectDetectionPort):
    """Adapter for Google Vision ``OBJECT_LOCALIZATION`` via the nginx proxy."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_GOOGLE_VISION_PROXY_URL,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        model_id: str = "google-vision-object-localization",
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._model_id = model_id

    def detect_objects(
        self, request: ObjectDetectionBatchRequest
    ) -> ObjectDetectionBatchResult:
        try:
            response = self._client.post(
                self._base_url,
                json=self.to_vendor_request(request),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            return self._failed_batch(request, self.from_vendor_error(exc))
        except (httpx.HTTPError, ValueError) as exc:
            return self._failed_batch(request, self.from_vendor_error(exc))
        return self.from_vendor_response(request, data)

    def to_vendor_request(self, request: ObjectDetectionBatchRequest) -> dict[str, Any]:
        vendor_requests: list[dict[str, Any]] = []
        max_results = request.options.max_results
        for image in request.images:
            image_payload: dict[str, Any]
            if image.kind == "bytes_base64":
                image_payload = {"content": image.content_base64}
            elif image.kind == "uri":
                image_payload = {"source": {"imageUri": image.uri}}
            else:
                raise ValueError("Google Vision adapter requires bytes_base64 or uri input")
            feature: dict[str, Any] = {"type": "OBJECT_LOCALIZATION"}
            if max_results is not None:
                feature["maxResults"] = max_results
            vendor_requests.append({"image": image_payload, "features": [feature]})
        return {"requests": vendor_requests}

    def from_vendor_response(
        self,
        request: ObjectDetectionBatchRequest,
        response: dict[str, Any],
    ) -> ObjectDetectionBatchResult:
        responses = response.get("responses")
        if not isinstance(responses, list):
            return self._failed_batch(
                request,
                ObjectDetectionError(
                    code="object_detection_schema_validation_failed",
                    message="Google Vision response missing responses array",
                    retryable=False,
                ),
            )
        results: list[ObjectDetectionImageResult] = []
        for idx, image in enumerate(request.images):
            item = responses[idx] if idx < len(responses) and isinstance(responses[idx], dict) else {}
            if "error" in item:
                error_obj = item.get("error") or {}
                results.append(
                    ObjectDetectionImageResult(
                        image_id=image.image_id,
                        image=self._image_metadata(image),
                        status="failed",
                        detections=[],
                        error=ObjectDetectionError(
                            code="object_detection_provider_error",
                            message=str(error_obj.get("message", "Google Vision image error"))[:500],
                            retryable=False,
                            provider_status=error_obj.get("status"),
                            provider_code=error_obj.get("code"),
                        ),
                    )
                )
                continue
            annotations = item.get("localizedObjectAnnotations", [])
            detections = [
                self._annotation_to_detection(image.image_id, ann, det_idx)
                for det_idx, ann in enumerate(annotations)
                if isinstance(ann, dict)
            ]
            results.append(
                ObjectDetectionImageResult(
                    image_id=image.image_id,
                    image=self._image_metadata(image),
                    status="succeeded",
                    detections=detections,
                )
            )
        return ObjectDetectionBatchResult(
            request_id=request.request_id,
            provider="google_vision",
            model_id=self._model_id,
            results=results,
            usage=ObjectDetectionUsage(
                image_count=len(request.images), provider_request_count=1
            ),
        )

    def from_vendor_error(self, error: Exception) -> ObjectDetectionError:
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            code = "object_detection_rate_limited" if status == 429 else "object_detection_provider_error"
            return ObjectDetectionError(
                code=code,  # type: ignore[arg-type]
                message=f"Google Vision HTTP error {status}",
                retryable=status == 429 or status >= 500,
                provider_code=status,
            )
        if isinstance(error, httpx.TimeoutException):
            return ObjectDetectionError(
                code="dependency_unavailable",
                message="Google Vision request timed out",
                retryable=True,
            )
        return ObjectDetectionError(
            code="object_detection_provider_error",
            message=str(error)[:500] or "Google Vision provider error",
            retryable=True,
        )

    def _annotation_to_detection(
        self, image_id: str, annotation: dict[str, Any], index: int
    ) -> ObjectDetectionItem:
        vertices = annotation.get("boundingPoly", {}).get("normalizedVertices", [])
        polygon = [Point2D(x=float(v.get("x", 0.0)), y=float(v.get("y", 0.0))) for v in vertices]
        bbox = BoundingBox(
            coordinate_space="normalized",
            format="polygon",
            polygon=polygon,
            xywh=polygon_to_xywh(polygon),
        )
        label = str(annotation.get("name", "object"))
        mid = annotation.get("mid")
        return ObjectDetectionItem(
            detection_id=f"{image_id}:google_vision:{index}",
            label=label,
            confidence=float(annotation.get("score", 0.0)),
            bbox=bbox,
            category=DetectionCategory(
                provider_category_id=mid,
                display_name=label,
                taxonomy="google_kg_mid" if mid else None,
            ),
            source_provider="google_vision",
            provider_detection_id=mid,
            attributes={"google_mid": mid} if mid else {},
        )

    def _failed_batch(
        self, request: ObjectDetectionBatchRequest, error: ObjectDetectionError
    ) -> ObjectDetectionBatchResult:
        return ObjectDetectionBatchResult(
            request_id=request.request_id,
            provider="google_vision",
            model_id=self._model_id,
            results=[
                ObjectDetectionImageResult(
                    image_id=image.image_id,
                    image=self._image_metadata(image),
                    status="failed",
                    detections=[],
                    error=error,
                )
                for image in request.images
            ],
            usage=ObjectDetectionUsage(image_count=len(request.images), provider_request_count=1),
        )

    @staticmethod
    def _image_metadata(image: Any) -> ObjectDetectionImageMetadata:
        return ObjectDetectionImageMetadata(
            width_px=image.width_px,
            height_px=image.height_px,
            frame_index=image.frame_index,
            timestamp_ms=image.timestamp_ms,
            sha256=image.sha256,
        )
