-- Data model consolidation for the gasket project (v1.0)
-- Core: refrigerator_products, product_gasket_specs, gasket_catalog

-- ---------------------------------------------------------------------------
-- 1) Core products table (refrigerator_models)
-- ---------------------------------------------------------------------------
ALTER TABLE public.refrigerator_products
  ADD COLUMN IF NOT EXISTS model text,
  ADD COLUMN IF NOT EXISTS brand text,
  ADD COLUMN IF NOT EXISTS manufacturer text,
  ADD COLUMN IF NOT EXISTS door_count integer,
  ADD COLUMN IF NOT EXISTS door_positions jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS manufacture_status text,
  ADD COLUMN IF NOT EXISTS status text DEFAULT 'new',
  ADD COLUMN IF NOT EXISTS product_image_url text,
  ADD COLUMN IF NOT EXISTS manufacture_date text,
  ADD COLUMN IF NOT EXISTS confidence_score numeric(5,2) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS source_summary jsonb DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

-- Maintain compatibility with existing naming used by older workflow
-- (keep model/equipment_model in sync when possible, do not fail if either is missing).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='refrigerator_products' AND column_name='equipment_model'
  ) THEN
    UPDATE public.refrigerator_products
    SET model = COALESCE(model, equipment_model)
    WHERE model IS NULL OR model = '';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='refrigerator_products' AND column_name='data_status'
  ) THEN
    UPDATE public.refrigerator_products
    SET status = COALESCE(status, data_status)
    WHERE status IS NULL OR status = '';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='refrigerator_products' AND column_name='product_image'
  ) THEN
    UPDATE public.refrigerator_products
    SET product_image_url = COALESCE(product_image_url, product_image)
    WHERE product_image_url IS NULL OR product_image_url = '';
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS refrigerator_products_brand_model_idx
  ON public.refrigerator_products (lower(brand), lower(model));

CREATE INDEX IF NOT EXISTS refrigerator_products_status_idx
  ON public.refrigerator_products (status);

CREATE INDEX IF NOT EXISTS refrigerator_products_confidence_idx
  ON public.refrigerator_products (confidence_score DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS refrigerator_products_has_image_idx
  ON public.refrigerator_products (id)
  WHERE product_image_url IS NOT NULL AND btrim(product_image_url) <> '';


-- ---------------------------------------------------------------------------
-- 2) Door-level gasket specs per product
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.product_gasket_specs (
  id bigserial PRIMARY KEY,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  door_position text NOT NULL,
  part_number text,
  width_in numeric(10,3),
  height_in numeric(10,3),
  color text,
  install_type text,
  profile_type text,
  price_band numeric(10,2),
  source_url text,
  source_summary jsonb,
  confidence_score numeric(5,2) DEFAULT 0,
  needs_customer_confirmation boolean DEFAULT true,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname='public' AND indexname='product_gasket_specs_product_door_part_source_uq'
  ) THEN
    CREATE UNIQUE INDEX product_gasket_specs_product_door_part_source_uq
      ON public.product_gasket_specs (refrigerator_product_id, door_position, part_number, source_url);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS product_gasket_specs_product_idx
  ON public.product_gasket_specs (refrigerator_product_id);

CREATE INDEX IF NOT EXISTS product_gasket_specs_door_idx
  ON public.product_gasket_specs (door_position);

CREATE INDEX IF NOT EXISTS product_gasket_specs_confidence_idx
  ON public.product_gasket_specs (confidence_score DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS product_gasket_specs_has_size_idx
  ON public.product_gasket_specs (refrigerator_product_id)
  WHERE width_in IS NOT NULL AND height_in IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3) Standard gasket library
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gasket_catalog (
  id bigserial PRIMARY KEY,
  profile_type text NOT NULL,
  style_name text,
  style_code text,
  nominal_width_in numeric(8,3),
  nominal_height_in numeric(8,3),
  notes text,
  image_url text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname='public' AND indexname='gasket_catalog_style_norm_uq'
  ) THEN
    CREATE UNIQUE INDEX gasket_catalog_style_norm_uq
      ON public.gasket_catalog (profile_type, COALESCE(style_code, ''), COALESCE(style_name, ''));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS gasket_catalog_profile_idx
  ON public.gasket_catalog (profile_type);

CREATE INDEX IF NOT EXISTS gasket_catalog_style_idx
  ON public.gasket_catalog (style_name);

CREATE INDEX IF NOT EXISTS gasket_catalog_size_idx
  ON public.gasket_catalog (nominal_width_in, nominal_height_in)
  WHERE nominal_width_in IS NOT NULL AND nominal_height_in IS NOT NULL;
