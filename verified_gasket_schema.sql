ALTER TABLE public.gasket_details
ADD COLUMN IF NOT EXISTS is_verified boolean DEFAULT false,
ADD COLUMN IF NOT EXISTS verified_at timestamptz,
ADD COLUMN IF NOT EXISTS verified_by text,
ADD COLUMN IF NOT EXISTS verification_note text;

ALTER TABLE public.refrigerator_products
ADD COLUMN IF NOT EXISTS verified_gasket_detail_id bigint REFERENCES public.gasket_details(id),
ADD COLUMN IF NOT EXISTS gasket_verified_at timestamptz;

CREATE TABLE IF NOT EXISTS public.gasket_verification_events (
  id bigserial PRIMARY KEY,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  gasket_detail_id bigint NOT NULL REFERENCES public.gasket_details(id) ON DELETE CASCADE,
  request_id bigint REFERENCES public.gasket_requests(id) ON DELETE SET NULL,
  verified_by text,
  verification_note text,
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS refrigerator_products_verified_gasket_idx
ON public.refrigerator_products (verified_gasket_detail_id);
