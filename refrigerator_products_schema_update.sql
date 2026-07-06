ALTER TABLE public.refrigerator_products
ADD COLUMN IF NOT EXISTS manufacturer text,
ADD COLUMN IF NOT EXISTS manufacture_date_text text;

ALTER TABLE public.gasket_requests
ADD COLUMN IF NOT EXISTS manufacturer text,
ADD COLUMN IF NOT EXISTS manufacture_date_text text;
