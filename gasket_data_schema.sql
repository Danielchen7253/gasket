ALTER TABLE public.commercial_gaskets
ADD COLUMN IF NOT EXISTS refrigerator_image_url text,
ADD COLUMN IF NOT EXISTS data_status text DEFAULT 'pending';

CREATE TABLE IF NOT EXISTS public.gasket_details (
  id bigserial PRIMARY KEY,
  commercial_gasket_id bigint NOT NULL REFERENCES public.commercial_gaskets(id) ON DELETE CASCADE,
  gasket_part_number text,
  gasket_name text,
  door_position text,
  width_in numeric(10,3),
  height_in numeric(10,3),
  dimensions_text text,
  gasket_profile text,
  refrigerator_image_url text,
  gasket_image_url text,
  profile_image_url text,
  source_url text NOT NULL,
  source_name text,
  confidence_score numeric(5,2) DEFAULT 0,
  scraped_at timestamptz DEFAULT now(),
  UNIQUE (commercial_gasket_id, source_url, gasket_part_number)
);

CREATE INDEX IF NOT EXISTS gasket_details_model_idx
ON public.gasket_details (commercial_gasket_id);

CREATE INDEX IF NOT EXISTS gasket_details_part_idx
ON public.gasket_details (gasket_part_number);

CREATE INDEX IF NOT EXISTS commercial_gaskets_data_status_idx
ON public.commercial_gaskets (data_status);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'gasket_details'
      AND policyname = 'service_role can manage gasket details'
  ) THEN
    CREATE POLICY "service_role can manage gasket details"
    ON public.gasket_details
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;
