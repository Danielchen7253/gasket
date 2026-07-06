create extension if not exists pgcrypto;

create table if not exists public.match_jobs (
  id bigserial primary key,
  request_id bigint unique,
  refrigerator_product_id bigint references public.refrigerator_products(id) on delete cascade,
  brand text not null,
  equipment_model text not null,
  job_status text not null default 'pending',
  pipeline_stage text not null default 'pending',
  missing_fields text[] default '{}',
  last_error text,
  last_heartbeat_at timestamptz default now(),
  next_retry_at timestamptz default now(),
  retry_count integer default 0,
  max_retries integer default 3,
  started_at timestamptz default now(),
  ended_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_match_jobs_product_id on public.match_jobs(refrigerator_product_id);
create index if not exists idx_match_jobs_status on public.match_jobs(job_status);
create index if not exists idx_match_jobs_updated_at on public.match_jobs(updated_at desc);
