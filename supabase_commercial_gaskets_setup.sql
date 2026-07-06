ALTER TABLE public.commercial_gaskets
ADD COLUMN IF NOT EXISTS source_url text,
ADD COLUMN IF NOT EXISTS scraped_at timestamptz DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS commercial_gaskets_brand_model_uidx
ON public.commercial_gaskets (brand, equipment_model);
