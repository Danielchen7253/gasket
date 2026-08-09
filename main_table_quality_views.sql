create or replace view public.refrigerator_product_quality
with (security_invoker = true)
as
select
  p.*,
  case
    when nullif(btrim(p.brand), '') is null
      or nullif(btrim(p.equipment_model), '') is null
      or lower(btrim(p.brand)) in ('test', 'unknown', 'n/a', 'none', 'tbd')
      or lower(btrim(p.equipment_model)) in ('test', 'unknown', 'n/a', 'none', 'tbd', 'model')
      or lower(btrim(p.equipment_model)) like 'test%'
      then 'invalid'
    when nullif(btrim(p.manufacturer), '') is not null
      and nullif(btrim(p.product_type), '') is not null
      and p.door_count between 1 and 8
      and nullif(btrim(p.door_layout), '') is not null
      and p.door_positions is not null
      and p.door_positions not in ('[]'::jsonb, '{}'::jsonb)
      and nullif(btrim(p.product_image_url), '') is not null
      and (
        nullif(btrim(p.official_product_url), '') is not null
        or nullif(btrim(p.spec_sheet_url), '') is not null
        or nullif(btrim(p.manual_url), '') is not null
      )
      and nullif(btrim(p.lifecycle_status), '') is not null
      and lower(btrim(p.lifecycle_status)) not in ('unknown', 'tbd')
      and nullif(btrim(p.data_source_summary), '') is not null
      and p.data_confidence is not null
      then 'complete'
    when p.door_count between 1 and 8
      and nullif(btrim(p.door_layout), '') is not null
      and p.door_positions is not null
      and p.door_positions not in ('[]'::jsonb, '{}'::jsonb)
      and nullif(btrim(p.product_image_url), '') is not null
      then 'customer_ready'
    when p.data_status in ('enriched', 'ai_structured', 'manual_research_structured', 'staff_verified')
      then 'researched'
    else 'identity_only'
  end as quality_level,
  array_remove(array[
    case when nullif(btrim(p.brand), '') is null then 'brand' end,
    case when nullif(btrim(p.equipment_model), '') is null then 'equipment_model' end,
    case when nullif(btrim(p.manufacturer), '') is null then 'manufacturer' end,
    case when nullif(btrim(p.product_type), '') is null then 'product_type' end,
    case when p.door_count is null or p.door_count not between 1 and 8 then 'door_count' end,
    case when nullif(btrim(p.door_layout), '') is null then 'door_layout' end,
    case when p.door_positions is null or p.door_positions in ('[]'::jsonb, '{}'::jsonb) then 'door_positions' end,
    case when nullif(btrim(p.product_image_url), '') is null then 'product_image_url' end,
    case when nullif(btrim(p.official_product_url), '') is null
      and nullif(btrim(p.spec_sheet_url), '') is null
      and nullif(btrim(p.manual_url), '') is null then 'primary_document' end,
    case when nullif(btrim(p.lifecycle_status), '') is null or lower(btrim(p.lifecycle_status)) in ('unknown', 'tbd') then 'lifecycle_status' end,
    case when p.data_confidence is null then 'data_confidence' end,
    case when nullif(btrim(p.data_source_summary), '') is null then 'data_source_summary' end
  ], null) as missing_fields,
  round((
    (case when nullif(btrim(p.brand), '') is not null then 1 else 0 end) +
    (case when nullif(btrim(p.equipment_model), '') is not null then 1 else 0 end) +
    (case when nullif(btrim(p.manufacturer), '') is not null then 1 else 0 end) +
    (case when nullif(btrim(p.product_type), '') is not null then 1 else 0 end) +
    (case when p.door_count between 1 and 8 then 1 else 0 end) +
    (case when nullif(btrim(p.door_layout), '') is not null then 1 else 0 end) +
    (case when p.door_positions is not null and p.door_positions not in ('[]'::jsonb, '{}'::jsonb) then 1 else 0 end) +
    (case when nullif(btrim(p.product_image_url), '') is not null then 1 else 0 end) +
    (case when nullif(btrim(p.official_product_url), '') is not null or nullif(btrim(p.spec_sheet_url), '') is not null or nullif(btrim(p.manual_url), '') is not null then 1 else 0 end) +
    (case when nullif(btrim(p.lifecycle_status), '') is not null and lower(btrim(p.lifecycle_status)) not in ('unknown', 'tbd') then 1 else 0 end) +
    (case when p.data_confidence is not null then 1 else 0 end) +
    (case when nullif(btrim(p.data_source_summary), '') is not null then 1 else 0 end)
  ) * 100.0 / 12, 1) as completeness_percent
from public.refrigerator_products p;

create or replace view public.refrigerator_product_quality_summary
with (security_invoker = true)
as
select
  count(*)::bigint as total_products,
  count(*) filter (where quality_level <> 'invalid')::bigint as valid_products,
  count(*) filter (where quality_level = 'invalid')::bigint as invalid_products,
  count(*) filter (where quality_level = 'identity_only')::bigint as identity_only_products,
  count(*) filter (where quality_level = 'researched')::bigint as researched_products,
  count(*) filter (where quality_level = 'customer_ready')::bigint as customer_ready_products,
  count(*) filter (where quality_level = 'complete')::bigint as complete_products,
  count(*) filter (where nullif(btrim(manufacturer), '') is not null)::bigint as manufacturer_filled,
  count(*) filter (where nullif(btrim(product_type), '') is not null)::bigint as product_type_filled,
  count(*) filter (where door_count between 1 and 8)::bigint as door_count_filled,
  count(*) filter (where nullif(btrim(door_layout), '') is not null)::bigint as door_layout_filled,
  count(*) filter (where door_positions is not null and door_positions not in ('[]'::jsonb, '{}'::jsonb))::bigint as door_positions_filled,
  count(*) filter (where nullif(btrim(product_image_url), '') is not null)::bigint as image_filled,
  count(*) filter (where nullif(btrim(official_product_url), '') is not null or nullif(btrim(spec_sheet_url), '') is not null or nullif(btrim(manual_url), '') is not null)::bigint as document_filled,
  count(*) filter (where nullif(btrim(lifecycle_status), '') is not null and lower(btrim(lifecycle_status)) not in ('unknown', 'tbd'))::bigint as lifecycle_filled,
  count(*) filter (where data_confidence is not null)::bigint as confidence_filled,
  count(*) filter (where nullif(btrim(data_source_summary), '') is not null)::bigint as source_summary_filled
from public.refrigerator_product_quality;

revoke all on public.refrigerator_product_quality from anon, authenticated;
revoke all on public.refrigerator_product_quality_summary from anon, authenticated;
grant select on public.refrigerator_product_quality to service_role;
grant select on public.refrigerator_product_quality_summary to service_role;
