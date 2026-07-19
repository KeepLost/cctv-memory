"""PostgreSQL schema DDL for the production backend.

SQLite keeps the SQLAlchemy ORM metadata path. PostgreSQL uses explicit DDL here
because the physical schema intentionally differs: JSONB instead of JSON text,
TIMESTAMPTZ policy, pgvector, and a PostgreSQL text-index projection table.
"""

from __future__ import annotations


def postgres_schema_ddl(*, vector_dimension: int) -> list[str]:
    """Return idempotent PostgreSQL DDL statements for the logical schema."""
    if vector_dimension <= 0:
        raise ValueError("vector_dimension must be positive")
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS principals (
          principal_id TEXT PRIMARY KEY,
          principal_type TEXT NOT NULL,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          external_subject_id TEXT,
          display_name TEXT NOT NULL,
          status TEXT NOT NULL,
          roles_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          groups_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_principals_tenant_status ON principals(tenant_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_principals_external_subject ON principals(external_subject_id)",
        """
        CREATE TABLE IF NOT EXISTS access_policies (
          access_policy_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          name TEXT NOT NULL,
          security_level TEXT NOT NULL,
          rules_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_policy_tenant_name UNIQUE(tenant_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS camera_locations (
          location_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          building TEXT,
          floor TEXT,
          area TEXT NOT NULL,
          room_or_zone TEXT,
          location_desc TEXT,
          access_policy_id TEXT,
          security_level TEXT NOT NULL DEFAULT 'internal',
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_locations_policy ON camera_locations(access_policy_id, security_level)",
        "CREATE INDEX IF NOT EXISTS idx_locations_area ON camera_locations(area)",
        """
        CREATE TABLE IF NOT EXISTS camera_devices (
          camera_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          camera_name TEXT NOT NULL,
          location_id TEXT NOT NULL,
          manufacturer TEXT,
          model TEXT,
          serial_number TEXT,
          install_position_desc TEXT,
          stream_uri TEXT,
          access_policy_id TEXT,
          status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_camera_location ON camera_devices(location_id)",
        "CREATE INDEX IF NOT EXISTS idx_camera_policy ON camera_devices(access_policy_id)",
        "CREATE INDEX IF NOT EXISTS idx_camera_status ON camera_devices(status)",
        """
        CREATE TABLE IF NOT EXISTS video_sources (
          video_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          source_type TEXT NOT NULL,
          source_uri TEXT NOT NULL,
          original_source_uri TEXT,
          camera_id TEXT NOT NULL,
          video_start_time TIMESTAMPTZ NOT NULL,
          video_end_time TIMESTAMPTZ,
          duration_ms INTEGER,
          source_status TEXT NOT NULL,
          external_source_id TEXT,
          access_policy_id TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_video_camera_starttime UNIQUE(camera_id, video_start_time)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_video_camera_time ON video_sources(camera_id, video_start_time, video_end_time)",
        "CREATE INDEX IF NOT EXISTS idx_video_policy ON video_sources(access_policy_id)",
        "CREATE INDEX IF NOT EXISTS idx_video_status ON video_sources(source_status)",
        """
        CREATE TABLE IF NOT EXISTS analysis_jobs (
          analysis_job_id TEXT PRIMARY KEY,
          video_id TEXT NOT NULL,
          job_status TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          analysis_options_json JSONB NOT NULL,
          model_version TEXT,
          prompt_version TEXT,
          pipeline_version TEXT,
          created_record_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          updated_record_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          archived_record_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          failed_segment_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          error_code TEXT,
          error_message TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_jobs_video ON analysis_jobs(video_id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON analysis_jobs(job_status, created_at)",
        """
        CREATE TABLE IF NOT EXISTS analysis_scale_tasks (
          scale_task_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          status TEXT NOT NULL,
          total_units INTEGER NOT NULL DEFAULT 0,
          succeeded_units INTEGER NOT NULL DEFAULT 0,
          failed_units INTEGER NOT NULL DEFAULT 0,
          skipped_reason TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          error_code TEXT,
          error_message TEXT,
          CONSTRAINT uq_scaletask_job_scale UNIQUE(analysis_job_id, analysis_scale)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS high_freq_triggers (
          trigger_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          scale_task_id TEXT NOT NULL,
          video_id TEXT NOT NULL,
          trigger_start_ms INTEGER NOT NULL,
          trigger_end_ms INTEGER NOT NULL,
          motion_score DOUBLE PRECISION,
          change_score DOUBLE PRECISION,
          trigger_reason TEXT NOT NULL,
          status TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          error_code TEXT,
          error_message TEXT,
          CONSTRAINT ck_trigger_time_order CHECK (trigger_start_ms < trigger_end_ms)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS analysis_units (
          unit_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          scale_task_id TEXT NOT NULL,
          video_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          unit_kind TEXT NOT NULL,
          segment_start_ms INTEGER NOT NULL,
          segment_end_ms INTEGER NOT NULL,
          window_index INTEGER NOT NULL,
          trigger_id TEXT,
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 1,
          last_error_code TEXT,
          last_error_message TEXT,
          latest_model_call_id TEXT,
          successful_model_call_id TEXT,
          produced_record_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          idempotency_key TEXT NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          CONSTRAINT ck_unit_time_order CHECK (segment_start_ms < segment_end_ms)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_units_scale_status ON analysis_units(scale_task_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_units_job_scale ON analysis_units(analysis_job_id, analysis_scale)",
        "CREATE INDEX IF NOT EXISTS idx_units_status_started ON analysis_units(status, started_at)",
        """
        CREATE TABLE IF NOT EXISTS model_call_logs (
          model_call_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          scale_task_id TEXT NOT NULL,
          unit_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          segment_start_ms INTEGER NOT NULL,
          segment_end_ms INTEGER NOT NULL,
          provider TEXT NOT NULL,
          model_id TEXT,
          prompt_version TEXT,
          pipeline_version TEXT,
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          error_type TEXT,
          error_message TEXT,
          raw_text_input TEXT,
          raw_text_output TEXT,
          parsed_output_json JSONB,
          validation_status TEXT,
          payload_hash TEXT,
          response_hash TEXT,
          media_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          attempt_details_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          duration_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_model_calls_unit ON model_call_logs(unit_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_model_calls_job ON model_call_logs(analysis_job_id, analysis_scale)",
        """
        CREATE TABLE IF NOT EXISTS detector_gate_logs (
          gate_log_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          scale_task_id TEXT NOT NULL,
          unit_id TEXT NOT NULL,
          video_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          segment_start_ms INTEGER NOT NULL,
          segment_end_ms INTEGER NOT NULL,
          provider TEXT NOT NULL,
          model_id TEXT,
          status TEXT NOT NULL,
          decision_json JSONB NOT NULL,
          frame_evidence_json JSONB NOT NULL,
          evidence_hash TEXT NOT NULL,
          rule_config_hash TEXT,
          media_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          artifact_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          duration_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_gate_time_order CHECK (segment_start_ms < segment_end_ms)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_detector_gate_unit ON detector_gate_logs(unit_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_detector_gate_job ON detector_gate_logs(analysis_job_id, analysis_scale)",
        """
        CREATE TABLE IF NOT EXISTS pre_vlm_gate_logs (
          gate_log_id TEXT PRIMARY KEY,
          analysis_job_id TEXT NOT NULL,
          scale_task_id TEXT NOT NULL,
          unit_id TEXT NOT NULL,
          video_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          unit_kind TEXT NOT NULL,
          profile_name TEXT NOT NULL,
          segment_start_ms INTEGER NOT NULL,
          segment_end_ms INTEGER NOT NULL,
          provider TEXT NOT NULL,
          model_id TEXT,
          status TEXT NOT NULL,
          error_type TEXT,
          error_message TEXT,
          raw_text_output TEXT,
          parsed_output_json JSONB,
          validation_status TEXT,
          attempt_details_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          decision_json JSONB NOT NULL,
          signals_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          frame_evidence_json JSONB NOT NULL,
          evidence_hash TEXT NOT NULL,
          rule_config_hash TEXT,
          suppression_policy TEXT,
          media_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          artifact_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          duration_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_pre_vlm_gate_time_order CHECK (segment_start_ms < segment_end_ms)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pre_vlm_gate_unit ON pre_vlm_gate_logs(unit_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_pre_vlm_gate_job ON pre_vlm_gate_logs(analysis_job_id, analysis_scale)",
        """
        CREATE TABLE IF NOT EXISTS analysis_timeline_events (
          timeline_event_id TEXT PRIMARY KEY,
          trace_id TEXT NOT NULL,
          span_id TEXT,
          parent_span_id TEXT,
          analysis_job_id TEXT,
          task_id TEXT,
          scale_task_id TEXT,
          unit_id TEXT,
          model_call_id TEXT,
          video_id TEXT,
          analysis_scale TEXT,
          unit_kind TEXT,
          segment_start_ms INTEGER,
          segment_end_ms INTEGER,
          event_name TEXT NOT NULL,
          event_phase TEXT NOT NULL,
          status TEXT,
          attempt_count INTEGER,
          occurred_at TIMESTAMPTZ NOT NULL,
          duration_ms INTEGER,
          error_code TEXT,
          error_message TEXT,
          correlation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_timeline_job_time ON analysis_timeline_events(analysis_job_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_timeline_unit_time ON analysis_timeline_events(unit_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_timeline_model_call ON analysis_timeline_events(model_call_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_timeline_trace_time ON analysis_timeline_events(trace_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_timeline_event_name_time ON analysis_timeline_events(event_name, occurred_at)",
        """
        CREATE TABLE IF NOT EXISTS observation_records (
          record_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_default',
          video_id TEXT NOT NULL,
          analysis_job_id TEXT NOT NULL,
          analysis_scale TEXT NOT NULL,
          segment_start_ms INTEGER NOT NULL,
          segment_end_ms INTEGER NOT NULL,
          observed_start_time TIMESTAMPTZ NOT NULL,
          observed_end_time TIMESTAMPTZ NOT NULL,
          camera_id TEXT NOT NULL,
          location_id TEXT NOT NULL,
          static_description_text TEXT NOT NULL,
          dynamic_description_text TEXT NOT NULL,
          tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          clip_uri TEXT,
          thumbnail_uri TEXT,
          attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          access_policy_id TEXT NOT NULL,
          security_level TEXT NOT NULL,
          model_version TEXT,
          prompt_version TEXT,
          pipeline_version TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_obs_time_order CHECK (segment_start_ms < segment_end_ms),
          CONSTRAINT uq_obs_segment_scale UNIQUE(video_id, segment_start_ms, segment_end_ms, analysis_scale)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_obs_video_time ON observation_records(video_id, segment_start_ms, segment_end_ms)",
        "CREATE INDEX IF NOT EXISTS idx_obs_observed_time ON observation_records(observed_start_time, observed_end_time)",
        "CREATE INDEX IF NOT EXISTS idx_obs_camera_time ON observation_records(camera_id, observed_start_time, observed_end_time)",
        "CREATE INDEX IF NOT EXISTS idx_obs_location_time ON observation_records(location_id, observed_start_time, observed_end_time)",
        "CREATE INDEX IF NOT EXISTS idx_obs_policy ON observation_records(access_policy_id, security_level)",
        "CREATE INDEX IF NOT EXISTS idx_obs_scale ON observation_records(analysis_scale)",
        """
        CREATE TABLE IF NOT EXISTS observation_record_history (
          history_id TEXT PRIMARY KEY,
          old_record_id TEXT NOT NULL,
          replaced_by_record_id TEXT,
          archived_by_analysis_job_id TEXT NOT NULL,
          archived_at TIMESTAMPTZ NOT NULL,
          archive_reason TEXT NOT NULL,
          record_snapshot_json JSONB NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS observation_vectors (
          record_id TEXT NOT NULL REFERENCES observation_records(record_id) ON DELETE CASCADE,
          vector_type TEXT NOT NULL,
          model_id TEXT NOT NULL,
          dimension INTEGER NOT NULL,
          embedding vector({vector_dimension}) NOT NULL,
          metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY(record_id, vector_type, model_id),
          CONSTRAINT ck_vector_dimension CHECK (dimension = {vector_dimension})
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_vectors_type_model ON observation_vectors(vector_type, model_id)",
        """
        CREATE TABLE IF NOT EXISTS observation_text_index (
          record_id TEXT NOT NULL REFERENCES observation_records(record_id) ON DELETE CASCADE,
          text_field TEXT NOT NULL,
          content TEXT NOT NULL,
          tsv tsvector NOT NULL,
          PRIMARY KEY(record_id, text_field)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_text_index_tsv ON observation_text_index USING gin(tsv)",
        "CREATE INDEX IF NOT EXISTS idx_text_index_content_trgm ON observation_text_index USING gin(content gin_trgm_ops)",
        """
        CREATE TABLE IF NOT EXISTS search_contexts (
          context_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          principal_id TEXT NOT NULL,
          session_id TEXT,
          authorized_scope_hash TEXT NOT NULL,
          dataset_revision TEXT NOT NULL,
          mode TEXT NOT NULL,
          default_revision_id TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          last_accessed_at TIMESTAMPTZ NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          status TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS search_revisions (
          revision_id TEXT PRIMARY KEY,
          context_id TEXT NOT NULL,
          parent_revision_id TEXT,
          op TEXT NOT NULL,
          op_params_json JSONB NOT NULL,
          candidate_count INTEGER NOT NULL,
          facets_json JSONB,
          created_at TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS search_candidates (
          revision_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          rank INTEGER NOT NULL,
          score DOUBLE PRECISION NOT NULL,
          score_detail_json JSONB NOT NULL,
          PRIMARY KEY(revision_id, record_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_candidates_revision_rank ON search_candidates(revision_id, rank)",
        """
        CREATE TABLE IF NOT EXISTS analysis_tasks (
          task_id TEXT PRIMARY KEY,
          schema_version TEXT NOT NULL,
          task_type TEXT NOT NULL,
          payload_json JSONB NOT NULL,
          status TEXT NOT NULL,
          priority INTEGER NOT NULL DEFAULT 0,
          retry_count INTEGER NOT NULL DEFAULT 0,
          max_retries INTEGER NOT NULL DEFAULT 3,
          next_run_at TIMESTAMPTZ NOT NULL,
          lease_owner TEXT,
          lease_expires_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          error_code TEXT,
          error_message TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_claim ON analysis_tasks(status, next_run_at, priority)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_lease ON analysis_tasks(lease_expires_at)",
        """
        CREATE TABLE IF NOT EXISTS audit_events (
          audit_event_id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          request_id TEXT,
          principal_id TEXT,
          session_id TEXT,
          context_id TEXT,
          resource_scope_hash TEXT,
          record_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          video_id TEXT,
          camera_id TEXT,
          metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_principal_time ON audit_events(principal_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_event_type_time ON audit_events(event_type, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(request_id)",
        """
        CREATE TABLE IF NOT EXISTS backup_jobs (
          backup_job_id TEXT PRIMARY KEY,
          backup_type TEXT NOT NULL,
          principal_id TEXT,
          status TEXT NOT NULL,
          manifest_json JSONB,
          created_at TIMESTAMPTZ NOT NULL,
          finished_at TIMESTAMPTZ,
          error_code TEXT,
          error_message TEXT
        )
        """,
        """
        INSERT INTO schema_metadata(key, value) VALUES ('schema_version', 'v1-postgres')
        ON CONFLICT (key) DO NOTHING
        """,
    ]
