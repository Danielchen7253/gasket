ALTER TABLE public.gasket_details
ADD COLUMN IF NOT EXISTS market_price_usd numeric(10, 2);

ALTER TABLE public.gasket_quote_items
ADD COLUMN IF NOT EXISTS market_price_usd numeric(10, 2);

DROP VIEW IF EXISTS public.refrigerator_product_quote_items;

CREATE OR REPLACE VIEW public.refrigerator_product_quote_items AS
WITH expanded AS (
  SELECT
    p.id AS refrigerator_product_id,
    p.brand,
    p.equipment_model,
    p.manufacturer,
    p.product_image_url,
    s.id AS product_gasket_spec_id,
    s.primary_part_number,
    s.universal_part_number,
    s.gasket_name AS spec_gasket_name,
    s.gasket_profile AS spec_gasket_profile,
    s.confidence_score AS spec_confidence_score,
    s.data_status AS spec_data_status,
    door.ordinality::integer AS door_index,
    door.value AS door_data
  FROM public.refrigerator_products p
  LEFT JOIN public.product_gasket_specs s
    ON s.refrigerator_product_id = p.id
  LEFT JOIN LATERAL jsonb_array_elements(COALESCE(s.doors, '[]'::jsonb)) WITH ORDINALITY AS door(value, ordinality)
    ON true
)
SELECT
  refrigerator_product_id,
  brand,
  equipment_model,
  manufacturer,
  product_image_url,
  product_gasket_spec_id,
  NULLIF(door_data->>'gasket_detail_id', '')::bigint AS gasket_detail_id,
  NULLIF(door_data->>'gasket_part_id', '')::bigint AS gasket_part_id,
  COALESCE(door_index, 1) AS door_index,
  NULLIF(door_data->>'door_position', '') AS door_position,
  COALESCE(NULLIF(door_data->>'gasket_name', ''), spec_gasket_name, 'Door gasket') AS gasket_name,
  COALESCE(NULLIF(door_data->>'part_number', ''), primary_part_number) AS part_number,
  COALESCE(NULLIF(door_data->>'universal_part_number', ''), universal_part_number) AS universal_part_number,
  NULLIF(door_data->>'width_in', '')::numeric AS width_in,
  NULLIF(door_data->>'height_in', '')::numeric AS height_in,
  CASE
    WHEN NULLIF(door_data->>'width_in', '') IS NOT NULL
     AND NULLIF(door_data->>'height_in', '') IS NOT NULL
    THEN ROUND((NULLIF(door_data->>'width_in', '')::numeric + NULLIF(door_data->>'height_in', '')::numeric) * 2, 2)
    ELSE NULL
  END AS perimeter_in,
  NULLIF(door_data->>'dimensions_text', '') AS dimensions_text,
  COALESCE(NULLIF(door_data->>'gasket_profile', ''), spec_gasket_profile) AS gasket_profile,
  NULLIF(door_data->>'gasket_image_url', '') AS gasket_image_url,
  NULLIF(door_data->>'profile_image_url', '') AS profile_image_url,
  NULLIF(door_data->>'source_url', '') AS source_url,
  NULLIF(door_data->>'source_name', '') AS source_name,
  COALESCE(NULLIF(door_data->>'confidence_score', '')::numeric, spec_confidence_score) AS confidence_score,
  public.gasket_base_price_for_perimeter(
    CASE
      WHEN NULLIF(door_data->>'width_in', '') IS NOT NULL
       AND NULLIF(door_data->>'height_in', '') IS NOT NULL
      THEN (NULLIF(door_data->>'width_in', '')::numeric + NULLIF(door_data->>'height_in', '')::numeric) * 2
      ELSE NULL
    END
  ) AS base_price_usd,
  NULLIF(door_data->>'market_price_usd', '')::numeric AS market_price_usd,
  public.gasket_final_price(
    public.gasket_base_price_for_perimeter(
      CASE
        WHEN NULLIF(door_data->>'width_in', '') IS NOT NULL
         AND NULLIF(door_data->>'height_in', '') IS NOT NULL
        THEN (NULLIF(door_data->>'width_in', '')::numeric + NULLIF(door_data->>'height_in', '')::numeric) * 2
        ELSE NULL
      END
    ),
    NULLIF(door_data->>'market_price_usd', '')::numeric
  ) AS final_price_usd,
  CASE
    WHEN NULLIF(door_data->>'market_price_usd', '') IS NOT NULL
    THEN 'Priced from size rule and adjusted below comparable parts-site price.'
    ELSE 'Priced from gasket perimeter size rule.'
  END AS pricing_note,
  spec_data_status
FROM expanded
WHERE product_gasket_spec_id IS NOT NULL
  AND door_data IS NOT NULL;

CREATE OR REPLACE FUNCTION public.refresh_product_quote_items(p_product_id bigint)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  affected integer;
BEGIN
  INSERT INTO public.gasket_quote_items (
    refrigerator_product_id,
    product_gasket_spec_id,
    gasket_detail_id,
    gasket_part_id,
    door_index,
    door_position,
    gasket_name,
    part_number,
    universal_part_number,
    width_in,
    height_in,
    perimeter_in,
    dimensions_text,
    gasket_profile,
    gasket_image_url,
    profile_image_url,
    source_url,
    source_name,
    confidence_score,
    base_price_usd,
    market_price_usd,
    final_price_usd,
    pricing_note,
    quote_status,
    updated_at
  )
  SELECT
    refrigerator_product_id,
    product_gasket_spec_id,
    gasket_detail_id,
    gasket_part_id,
    door_index,
    door_position,
    gasket_name,
    part_number,
    universal_part_number,
    width_in,
    height_in,
    perimeter_in,
    dimensions_text,
    gasket_profile,
    gasket_image_url,
    profile_image_url,
    source_url,
    source_name,
    confidence_score,
    base_price_usd,
    market_price_usd,
    final_price_usd,
    pricing_note,
    CASE WHEN spec_data_status = 'verified' THEN 'verified' ELSE 'candidate' END,
    now()
  FROM public.refrigerator_product_quote_items
  WHERE refrigerator_product_id = p_product_id
  ON CONFLICT (refrigerator_product_id, door_index)
  DO UPDATE SET
    product_gasket_spec_id = EXCLUDED.product_gasket_spec_id,
    gasket_detail_id = EXCLUDED.gasket_detail_id,
    gasket_part_id = EXCLUDED.gasket_part_id,
    door_position = EXCLUDED.door_position,
    gasket_name = EXCLUDED.gasket_name,
    part_number = EXCLUDED.part_number,
    universal_part_number = EXCLUDED.universal_part_number,
    width_in = EXCLUDED.width_in,
    height_in = EXCLUDED.height_in,
    perimeter_in = EXCLUDED.perimeter_in,
    dimensions_text = EXCLUDED.dimensions_text,
    gasket_profile = EXCLUDED.gasket_profile,
    gasket_image_url = EXCLUDED.gasket_image_url,
    profile_image_url = EXCLUDED.profile_image_url,
    source_url = EXCLUDED.source_url,
    source_name = EXCLUDED.source_name,
    confidence_score = EXCLUDED.confidence_score,
    base_price_usd = EXCLUDED.base_price_usd,
    market_price_usd = COALESCE(public.gasket_quote_items.market_price_usd, EXCLUDED.market_price_usd),
    final_price_usd = public.gasket_final_price(EXCLUDED.base_price_usd, COALESCE(public.gasket_quote_items.market_price_usd, EXCLUDED.market_price_usd)),
    pricing_note = EXCLUDED.pricing_note,
    quote_status = EXCLUDED.quote_status,
    updated_at = now();

  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;
