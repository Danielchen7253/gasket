ALTER TABLE public.refrigerator_products
  ADD COLUMN IF NOT EXISTS door_count integer,
  ADD COLUMN IF NOT EXISTS door_layout text,
  ADD COLUMN IF NOT EXISTS door_positions jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS door_layout_confidence numeric,
  ADD COLUMN IF NOT EXISTS door_layout_source text,
  ADD COLUMN IF NOT EXISTS door_layout_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS refrigerator_products_door_count_idx
ON public.refrigerator_products (door_count);
