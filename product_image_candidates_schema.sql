CREATE TABLE IF NOT EXISTS public.product_image_candidates (
  id bigserial PRIMARY KEY,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  image_url text NOT NULL,
  page_url text,
  source_name text,
  image_title text,
  image_width integer,
  image_height integer,
  match_score numeric(5,2) DEFAULT 0,
  evidence jsonb DEFAULT '{}'::jsonb,
  is_selected boolean DEFAULT false,
  created_at timestamptz DEFAULT now(),
  UNIQUE (refrigerator_product_id, image_url)
);

CREATE INDEX IF NOT EXISTS product_image_candidates_product_idx
ON public.product_image_candidates (refrigerator_product_id);

CREATE INDEX IF NOT EXISTS product_image_candidates_score_idx
ON public.product_image_candidates (match_score DESC);

ALTER TABLE public.refrigerator_products
ADD COLUMN IF NOT EXISTS product_image_confidence numeric(5,2),
ADD COLUMN IF NOT EXISTS product_image_source_url text,
ADD COLUMN IF NOT EXISTS product_image_verified boolean DEFAULT false;
