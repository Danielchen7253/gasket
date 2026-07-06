-- Match job pipeline observability fields (phase timing, error reason chain, retries)
ALTER TABLE public.match_jobs
  ADD COLUMN IF NOT EXISTS pipeline_stage_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS pipeline_stage_error_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS pipeline_stage_duration_ms integer,
  ADD COLUMN IF NOT EXISTS last_error_stage text,
  ADD COLUMN IF NOT EXISTS source_system text DEFAULT 'webapp';

CREATE INDEX IF NOT EXISTS match_jobs_stage_started_at_idx
  ON public.match_jobs (pipeline_stage_started_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS match_jobs_request_id_idx
  ON public.match_jobs (request_id);
