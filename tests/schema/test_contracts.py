"""Schema validation tests for key contracts (testing-contract §2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from cctv_memory.contracts.analysis import HighFreqTrigger
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.common import TimeRange
from cctv_memory.contracts.search import StartObservationSearchRequest
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel, TriggerStatus
from pydantic import ValidationError

_TZ = timezone(timedelta(hours=8))


class TestTimeRange:
    def test_requires_timezone(self) -> None:
        naive = datetime(2026, 6, 6, 21, 0, 0)  # noqa: DTZ001
        with pytest.raises(ValidationError):
            TimeRange(start=naive, end=naive + timedelta(hours=1))

    def test_start_before_end(self) -> None:
        start = datetime(2026, 6, 6, 22, 0, 0, tzinfo=_TZ)
        end = datetime(2026, 6, 6, 21, 0, 0, tzinfo=_TZ)
        with pytest.raises(ValidationError):
            TimeRange(start=start, end=end)

    def test_valid_range(self) -> None:
        start = datetime(2026, 6, 6, 21, 0, 0, tzinfo=_TZ)
        end = datetime(2026, 6, 6, 22, 0, 0, tzinfo=_TZ)
        tr = TimeRange(start=start, end=end)
        assert tr.start < tr.end


class TestHighFreqTriggerKey:
    def _build(self, key: str) -> HighFreqTrigger:
        return HighFreqTrigger(
            trigger_id="trigger_001",
            analysis_job_id="job_001",
            scale_task_id="scale_001",
            video_id="video_001",
            trigger_start_ms=120000,
            trigger_end_ms=130000,
            trigger_reason="motion_spike",
            status=TriggerStatus.PENDING,
            idempotency_key=key,
        )

    def test_key_includes_video_id(self) -> None:
        key = HighFreqTrigger.build_idempotency_key(
            "job_001", "video_001", 120000, 130000, "motion_spike"
        )
        assert key == "job_001:video_001:120000:130000:motion_spike"
        trigger = self._build(key)
        assert trigger.video_id in trigger.idempotency_key

    def test_key_without_video_id_rejected(self) -> None:
        bad_key = "job_001:120000:130000:motion_spike"
        with pytest.raises(ValidationError):
            self._build(bad_key)

    def test_start_before_end(self) -> None:
        key = HighFreqTrigger.build_idempotency_key(
            "job_001", "video_001", 130000, 120000, "motion_spike"
        )
        with pytest.raises(ValidationError):
            HighFreqTrigger(
                trigger_id="trigger_001",
                analysis_job_id="job_001",
                scale_task_id="scale_001",
                video_id="video_001",
                trigger_start_ms=130000,
                trigger_end_ms=120000,
                trigger_reason="motion_spike",
                idempotency_key=key,
            )


class TestStartObservationSearchRequest:
    def test_includes_required_fields(self) -> None:
        req = StartObservationSearchRequest()
        assert hasattr(req, "video_ids")
        assert hasattr(req, "analysis_scale_filter")
        assert hasattr(req, "scale_strategy")
        assert req.video_ids == []
        assert req.analysis_scale_filter == []
        assert req.scale_strategy is None

    def test_top_k_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            StartObservationSearchRequest(top_k=101)


class TestAuthorizedScopeEmptyMeansNoAccess:
    def _scope(self, **overrides: object) -> AuthorizedScope:
        base: dict[str, object] = {
            "tenant_id": "tenant_default",
            "principal_id": "user_001",
            "allowed_camera_ids": ["cam_lobby_01"],
            "allowed_location_ids": ["loc_lobby_01"],
            "allowed_access_policy_ids": ["policy_public_area"],
            "max_security_level": SecurityLevel.INTERNAL,
            "capabilities": [],
            "scope_hash": "scope_hash_abc",
        }
        base.update(overrides)
        return AuthorizedScope(**base)  # type: ignore[arg-type]

    def test_permits_when_all_dimensions_match(self) -> None:
        scope = self._scope()
        assert scope.permits_resource(
            tenant_id="tenant_default",
            camera_id="cam_lobby_01",
            location_id="loc_lobby_01",
            access_policy_id="policy_public_area",
            security_level=SecurityLevel.INTERNAL,
        )

    def test_empty_camera_ids_denies(self) -> None:
        scope = self._scope(allowed_camera_ids=[])
        assert not scope.permits_resource(
            tenant_id="tenant_default",
            camera_id="cam_lobby_01",
            location_id="loc_lobby_01",
            access_policy_id="policy_public_area",
            security_level=SecurityLevel.INTERNAL,
        )

    def test_security_level_above_max_denied(self) -> None:
        scope = self._scope()
        assert not scope.permits_resource(
            tenant_id="tenant_default",
            camera_id="cam_lobby_01",
            location_id="loc_lobby_01",
            access_policy_id="policy_public_area",
            security_level=SecurityLevel.CONFIDENTIAL,
        )

    def test_tenant_mismatch_denied(self) -> None:
        scope = self._scope()
        assert not scope.permits_resource(
            tenant_id="tenant_other",
            camera_id="cam_lobby_01",
            location_id="loc_lobby_01",
            access_policy_id="policy_public_area",
            security_level=SecurityLevel.INTERNAL,
        )


class TestSecurityLevelOrder:
    def test_order(self) -> None:
        assert (
            SecurityLevel.PUBLIC.rank
            < SecurityLevel.INTERNAL.rank
            < SecurityLevel.CONFIDENTIAL.rank
            < SecurityLevel.RESTRICTED.rank
        )

    def test_stricter(self) -> None:
        assert (
            SecurityLevel.stricter(SecurityLevel.PUBLIC, SecurityLevel.RESTRICTED)
            is SecurityLevel.RESTRICTED
        )


class TestVlmOutputForbidsPolicyFields:
    def test_valid_output(self) -> None:
        out = VlmObservationOutput(
            static="a",
            dynamic="b",
            tags=["person"],
        )
        assert out.attr.alert is False
        assert out.quality.score == 0.0

    def test_access_policy_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VlmObservationOutput(
                static="a",
                dynamic="b",
                access_policy_id="policy_x",  # type: ignore[call-arg]
            )

    def test_security_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VlmObservationOutput(
                static="a",
                dynamic="b",
                security_level="confidential",  # type: ignore[call-arg]
            )

    def test_missing_text_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VlmObservationOutput(static="a")  # type: ignore[call-arg]

    def test_legacy_fields_rejected(self) -> None:
        # Removed legacy fields must not be accepted any more (extra="forbid").
        for bad in ("schema_version", "uncertainties", "static_description_text"):
            with pytest.raises(ValidationError):
                VlmObservationOutput(static="a", dynamic="b", **{bad: "x"})  # type: ignore[arg-type]

    def test_alert_only_attr_field(self) -> None:
        with pytest.raises(ValidationError):
            VlmObservationOutput(
                static="a",
                dynamic="b",
                attr={"alert": True, "objects": []},  # extra key rejected
            )


def test_utc_helper_available() -> None:
    # Guard against accidental removal of stdlib UTC import usage.
    assert datetime.now(UTC).tzinfo is not None


class TestAnalysisTimelineEvent:
    def test_valid_event(self) -> None:
        event = AnalysisTimelineEvent(
            timeline_event_id="tl_001",
            trace_id="job_001",
            analysis_job_id="job_001",
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=0,
            segment_end_ms=12000,
            event_name="frame_select",
            event_phase="start",
            occurred_at=datetime.now(UTC),
            metadata={"frames_requested": 6},
        )
        assert event.event_phase == "start"
        assert event.metadata["frames_requested"] == 6

    def test_bad_phase_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisTimelineEvent(
                timeline_event_id="tl_bad",
                trace_id="job_001",
                event_name="frame_select",
                event_phase="middle",
                occurred_at=datetime.now(UTC),
            )

    def test_naive_time_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnalysisTimelineEvent(
                timeline_event_id="tl_bad_time",
                trace_id="job_001",
                event_name="frame_select",
                event_phase="instant",
                occurred_at=datetime(2026, 6, 24, 12, 0, 0),  # noqa: DTZ001
            )
