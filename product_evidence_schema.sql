CREATE TABLE IF NOT EXISTS public.product_evidence_packages (
  id bigserial PRIMARY KEY,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  brand text,
  equipment_model text,
  stage text NOT NULL DEFAULT 'current_result',
  status text NOT NULL DEFAULT 'collecting_evidence',
  overall_confidence numeric,
  completeness_score numeric,
  profile_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  current_best_product_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  current_best_gasket_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  conflict_items jsonb NOT NULL DEFAULT '[]'::jsonb,
  last_built_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (refrigerator_product_id)
);

CREATE INDEX IF NOT EXISTS product_evidence_packages_product_idx
ON public.product_evidence_packages (refrigerator_product_id);

CREATE INDEX IF NOT EXISTS product_evidence_packages_status_idx
ON public.product_evidence_packages (status);

CREATE TABLE IF NOT EXISTS public.product_evidence_items (
  id bigserial PRIMARY KEY,
  package_id bigint NOT NULL REFERENCES public.product_evidence_packages(id) ON DELETE CASCADE,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  evidence_type text NOT NULL,
  source_type text,
  source_name text,
  source_url text,
  field_name text,
  supports_value text,
  confidence_score numeric,
  evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  conflicts boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS product_evidence_items_product_idx
ON public.product_evidence_items (refrigerator_product_id);

CREATE INDEX IF NOT EXISTS product_evidence_items_package_idx
ON public.product_evidence_items (package_id);

CREATE INDEX IF NOT EXISTS product_evidence_items_field_idx
ON public.product_evidence_items (field_name);
