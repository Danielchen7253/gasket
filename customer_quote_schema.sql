CREATE TABLE IF NOT EXISTS public.gasket_price_rules (
  id bigserial PRIMARY KEY,
  max_perimeter_in numeric(10,2) NOT NULL UNIQUE,
  base_price_usd numeric(10,2) NOT NULL,
  label text,
  active boolean DEFAULT true,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

INSERT INTO public.gasket_price_rules (max_perimeter_in, base_price_usd, label)
VALUES
  (98, 45, 'Under 98 in'),
  (117, 68, 'Under 117 in'),
  (146, 90, 'Under 146 in'),
  (190, 120, 'Under 190 in')
ON CONFLICT (max_perimeter_in) DO UPDATE
SET base_price_usd = EXCLUDED.base_price_usd,
    label = EXCLUDED.label,
    active = true,
    updated_at = now();

CREATE TABLE IF NOT EXISTS public.gasket_quote_items (
  id bigserial PRIMARY KEY,
  request_id bigint REFERENCES public.gasket_requests(id) ON DELETE CASCADE,
  refrigerator_product_id bigint NOT NULL REFERENCES public.refrigerator_products(id) ON DELETE CASCADE,
  product_gasket_spec_id bigint REFERENCES public.product_gasket_specs(id) ON DELETE SET NULL,
  gasket_detail_id bigint REFERENCES public.gasket_details(id) ON DELETE SET NULL,
  gasket_part_id bigint REFERENCES public.gasket_parts(id) ON DELETE SET NULL,
  door_index integer NOT NULL DEFAULT 1,
  door_position text,
  gasket_name text,
  part_number text,
  universal_part_number text,
  width_in numeric(10,3),
  height_in numeric(10,3),
  perimeter_in numeric(10,3),
  dimensions_text text,
  gasket_profile text,
  gasket_image_url text,
  profile_image_url text,
  source_url text,
  source_name text,
  confidence_score numeric(5,2) DEFAULT 0,
  base_price_usd numeric(10,2),
  market_price_usd numeric(10,2),
  final_price_usd numeric(10,2),
  pricing_note text,
  shopify_product_id text,
  shopify_variant_id text,
  shopify_checkout_url text,
  selected boolean DEFAULT false,
  quote_status text DEFAULT 'draft',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  UNIQUE (request_id, refrigerator_product_id, door_index),
  UNIQUE (refrigerator_product_id, door_index)
);

CREATE INDEX IF NOT EXISTS gasket_quote_items_request_idx
ON public.gasket_quote_items (request_id);

CREATE INDEX IF NOT EXISTS gasket_quote_items_product_idx
ON public.gasket_quote_items (refrigerator_product_id);

CREATE OR REPLACE FUNCTION public.gasket_base_price_for_perimeter(p_perimeter numeric)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
  SELECT COALESCE(
    (
      SELECT base_price_usd
      FROM public.gasket_price_rules
      WHERE active = true
        AND p_perimeter IS NOT NULL
        AND p_perimeter < max_perimeter_in
      ORDER BY max_perimeter_in ASC
      LIMIT 1
    ),
    120
  );
$$;

CREATE OR REPLACE FUNCTION public.gasket_final_price(p_base numeric, p_market numeric)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
  SELECT
    CASE
      WHEN p_base IS NULL THEN NULL
      WHEN p_market IS NULL THEN p_base
      WHEN p_market > p_base THEN GREATEST(p_base, ROUND((p_market * 0.90)::numeric, 2))
      ELSE p_base
    END;
$$;

CREATE OR REPLACE VIEW public.refrigerator_product_quote_items AS
WITH expanded_doors AS (
  SELECT
    pgs.id AS product_gasket_spec_id,
    pgs.refrigerator_product_id,
    pgs.primary_gasket_part_id,
    pgs.primary_part_number,
    pgs.universal_part_number AS spec_universal_part_number,
    pgs.gasket_name AS spec_gasket_name,
    pgs.gasket_profile AS spec_gasket_profile,
    pgs.best_source_url,
    pgs.best_source_name,
    pgs.confidence_score AS spec_confidence_score,
    door.ordinality::integer AS door_index,
    door.value AS door
  FROM public.product_gasket_specs pgs
  LEFT JOIN LATERAL jsonb_array_elements(COALESCE(pgs.doors, '[]'::jsonb))
    WITH ORDINALITY AS door(value, ordinality)
    ON true
),
normalized AS (
  SELECT
    ed.refrigerator_product_id,
    ed.product_gasket_spec_id,
    NULLIF(ed.door_index, 0) AS door_index,
    (ed.door ->> 'door_position') AS door_position,
    COALESCE(ed.door ->> 'gasket_name', ed.spec_gasket_name) AS gasket_name,
    COALESCE(ed.door ->> 'part_number', ed.primary_part_number) AS part_number,
    COALESCE(ed.door ->> 'universal_part_number', ed.spec_universal_part_number) AS universal_part_number,
    NULLIF(ed.door ->> 'width_in', '')::numeric AS width_in,
    NULLIF(ed.door ->> 'height_in', '')::numeric AS height_in,
    COALESCE(ed.door ->> 'dimensions_text', '') AS dimensions_text,
    COALESCE(ed.door ->> 'gasket_profile', ed.spec_gasket_profile) AS gasket_profile,
    (ed.door ->> 'gasket_image_url') AS gasket_image_url,
    (ed.door ->> 'profile_image_url') AS profile_image_url,
    COALESCE(ed.door ->> 'source_url', ed.best_source_url) AS source_url,
    COALESCE(ed.door ->> 'source_name', ed.best_source_name) AS source_name,
    COALESCE(NULLIF(ed.door ->> 'confidence_score', '')::numeric, ed.spec_confidence_score, 0) AS confidence_score,
    ed.primary_gasket_part_id AS gasket_part_id
  FROM expanded_doors ed
  WHERE ed.door IS NOT NULL
)
SELECT
  row_number() OVER (PARTITION BY refrigerator_product_id ORDER BY confidence_score DESC, door_index ASC) AS quote_rank,
  refrigerator_product_id,
  product_gasket_spec_id,
  gasket_part_id,
  door_index,
  door_position,
  gasket_name,
  part_number,
  universal_part_number,
  width_in,
  height_in,
  CASE
    WHEN width_in IS NOT NULL AND height_in IS NOT NULL THEN ROUND((2 * (width_in + height_in))::numeric, 3)
    ELSE NULL
  END AS perimeter_in,
  dimensions_text,
  gasket_profile,
  gasket_image_url,
  profile_image_url,
  source_url,
  source_name,
  confidence_score,
  public.gasket_base_price_for_perimeter(
    CASE
      WHEN width_in IS NOT NULL AND height_in IS NOT NULL THEN 2 * (width_in + height_in)
      ELSE NULL
    END
  ) AS base_price_usd,
  NULL::numeric AS market_price_usd,
  public.gasket_base_price_for_perimeter(
    CASE
      WHEN width_in IS NOT NULL AND height_in IS NOT NULL THEN 2 * (width_in + height_in)
      ELSE NULL
    END
  ) AS final_price_usd,
  'Calculated from gasket perimeter pricing rule.'::text AS pricing_note
FROM normalized;

CREATE OR REPLACE FUNCTION public.refresh_product_quote_items(p_product_id bigint)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  inserted_count integer;
BEGIN
  INSERT INTO public.gasket_quote_items (
    refrigerator_product_id,
    product_gasket_spec_id,
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
    updated_at
  )
  SELECT
    refrigerator_product_id,
    product_gasket_spec_id,
    gasket_part_id,
    quote_rank::integer,
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
    now()
  FROM public.refrigerator_product_quote_items
  WHERE refrigerator_product_id = p_product_id
  ORDER BY quote_rank
  ON CONFLICT (refrigerator_product_id, door_index) DO UPDATE
  SET product_gasket_spec_id = EXCLUDED.product_gasket_spec_id,
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
      updated_at = now();

  GET DIAGNOSTICS inserted_count = ROW_COUNT;
  RETURN inserted_count;
END;
$$;
