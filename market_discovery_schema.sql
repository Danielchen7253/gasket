ALTER TABLE public.refrigerator_products
ADD COLUMN IF NOT EXISTS product_type text,
ADD COLUMN IF NOT EXISTS official_product_url text,
ADD COLUMN IF NOT EXISTS spec_sheet_url text,
ADD COLUMN IF NOT EXISTS manual_url text,
ADD COLUMN IF NOT EXISTS lifecycle_status text DEFAULT 'unknown',
ADD COLUMN IF NOT EXISTS lifecycle_evidence_url text,
ADD COLUMN IF NOT EXISTS model_year_start integer,
ADD COLUMN IF NOT EXISTS model_year_end integer,
ADD COLUMN IF NOT EXISTS data_confidence numeric(5,2),
ADD COLUMN IF NOT EXISTS last_discovered_at timestamptz,
ADD COLUMN IF NOT EXISTS last_enriched_at timestamptz;

CREATE TABLE IF NOT EXISTS public.discovered_refrigerator_models (
  id bigserial PRIMARY KEY,
  discovered_brand text NOT NULL,
  discovered_model text NOT NULL,
  normalized_brand text NOT NULL,
  normalized_model text NOT NULL,
  source_url text NOT NULL,
  source_name text,
  page_title text,
  evidence_text text,
  product_type text,
  product_image_url text,
  official_product_url text,
  spec_sheet_url text,
  manual_url text,
  lifecycle_status text DEFAULT 'unknown',
  lifecycle_evidence_url text,
  model_year_start integer,
  model_year_end integer,
  confidence_score numeric(5,2) DEFAULT 0,
  matched_existing_product_id bigint REFERENCES public.refrigerator_products(id) ON DELETE SET NULL,
  promoted_product_id bigint REFERENCES public.refrigerator_products(id) ON DELETE SET NULL,
  review_status text DEFAULT 'pending',
  evidence jsonb DEFAULT '{}'::jsonb,
  first_seen_at timestamptz DEFAULT now(),
  last_seen_at timestamptz DEFAULT now(),
  UNIQUE (normalized_brand, normalized_model, source_url)
);

CREATE INDEX IF NOT EXISTS discovered_models_brand_model_idx
ON public.discovered_refrigerator_models (normalized_brand, normalized_model);

CREATE INDEX IF NOT EXISTS discovered_models_score_idx
ON public.discovered_refrigerator_models (confidence_score DESC);

CREATE INDEX IF NOT EXISTS discovered_models_review_status_idx
ON public.discovered_refrigerator_models (review_status);
