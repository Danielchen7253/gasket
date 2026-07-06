CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS public.gasket_requests (
  id bigserial PRIMARY KEY,
  customer_name text,
  customer_phone text,
  customer_email text,
  nameplate_image_url text,
  ocr_text text,
  detected_brand text,
  detected_model text,
  detected_serial_number text,
  detected_manufacture_date date,
  matched_refrigerator_product_id bigint REFERENCES public.refrigerator_products(id),
  match_score numeric(5,2),
  status text DEFAULT 'new',
  customer_confirmed_at timestamptz,
  factory_sent_at timestamptz,
  notes text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.gasket_request_candidates (
  id bigserial PRIMARY KEY,
  request_id bigint NOT NULL REFERENCES public.gasket_requests(id) ON DELETE CASCADE,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id),
  match_score numeric(5,2),
  match_reason text,
  created_at timestamptz DEFAULT now(),
  UNIQUE (request_id, refrigerator_product_id)
);

CREATE OR REPLACE VIEW public.refrigerator_product_best_gasket AS
SELECT
  rp.id AS refrigerator_product_id,
  rp.brand,
  rp.equipment_model,
  rp.manufacture_date,
  rp.product_image_url,
  rp.data_status,
  gd.id AS gasket_detail_id,
  gd.gasket_part_number,
  gd.gasket_name,
  gd.door_position,
  gd.width_in,
  gd.height_in,
  gd.dimensions_text,
  gd.gasket_profile,
  gd.refrigerator_image_url,
  gd.gasket_image_url,
  gd.profile_image_url,
  gd.source_url,
  gd.source_name,
  gd.confidence_score
FROM public.refrigerator_products rp
LEFT JOIN LATERAL (
  SELECT *
  FROM public.gasket_details gd
  WHERE gd.refrigerator_product_id = rp.id
  ORDER BY
    gd.confidence_score DESC NULLS LAST,
    (gd.width_in IS NOT NULL AND gd.height_in IS NOT NULL) DESC,
    (gd.profile_image_url IS NOT NULL) DESC,
    (gd.gasket_image_url IS NOT NULL) DESC,
    gd.id ASC
  LIMIT 1
) gd ON true;

CREATE OR REPLACE FUNCTION public.search_refrigerator_products(
  q_brand text,
  q_model text,
  result_limit integer DEFAULT 10
)
RETURNS TABLE (
  refrigerator_product_id bigint,
  brand text,
  equipment_model text,
  product_image_url text,
  manufacture_date date,
  gasket_detail_id bigint,
  gasket_part_number text,
  dimensions_text text,
  width_in numeric,
  height_in numeric,
  gasket_image_url text,
  profile_image_url text,
  source_url text,
  source_name text,
  confidence_score numeric,
  match_score numeric
)
LANGUAGE sql
STABLE
AS $$
  SELECT
    v.refrigerator_product_id,
    v.brand,
    v.equipment_model,
    v.product_image_url,
    v.manufacture_date,
    v.gasket_detail_id,
    v.gasket_part_number,
    v.dimensions_text,
    v.width_in,
    v.height_in,
    v.gasket_image_url,
    v.profile_image_url,
    v.source_url,
    v.source_name,
    v.confidence_score,
    ROUND(
      (
        COALESCE(similarity(lower(v.brand), lower(COALESCE(q_brand, ''))), 0) * 35
        +
        GREATEST(
          COALESCE(similarity(upper(v.equipment_model), upper(COALESCE(q_model, ''))), 0),
          CASE
            WHEN upper(v.equipment_model) = upper(COALESCE(q_model, '')) THEN 1
            WHEN upper(v.equipment_model) LIKE '%' || upper(COALESCE(q_model, '')) || '%' THEN 0.85
            WHEN upper(COALESCE(q_model, '')) LIKE '%' || upper(v.equipment_model) || '%' THEN 0.75
            ELSE 0
          END
        ) * 65
      )::numeric,
      2
    ) AS match_score
  FROM public.refrigerator_product_best_gasket v
  WHERE
    (
      q_brand IS NULL
      OR q_brand = ''
      OR lower(v.brand) % lower(q_brand)
      OR lower(v.brand) LIKE '%' || lower(q_brand) || '%'
    )
    AND
    (
      q_model IS NULL
      OR q_model = ''
      OR upper(v.equipment_model) % upper(q_model)
      OR upper(v.equipment_model) LIKE '%' || upper(q_model) || '%'
      OR upper(q_model) LIKE '%' || upper(v.equipment_model) || '%'
    )
  ORDER BY match_score DESC, v.confidence_score DESC NULLS LAST
  LIMIT result_limit;
$$;

