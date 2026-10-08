import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const cwd = process.cwd();
const csvPath = path.join(cwd, "work", "gasket_import_sample.csv");
const outputDir = path.join(cwd, "outputs");
const outputPath = path.join(outputDir, "gasket_import_sample_100.xlsx");

function parseCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];
    if (ch === '"' && inQuotes && next === '"') {
      value += '"';
      i += 1;
    } else if (ch === '"') {
      inQuotes = !inQuotes;
    } else if (ch === "," && !inQuotes) {
      row.push(value);
      value = "";
    } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
      if (ch === "\r" && next === "\n") i += 1;
      row.push(value);
      if (row.some((cell) => cell !== "")) rows.push(row);
      row = [];
      value = "";
    } else {
      value += ch;
    }
  }
  if (value || row.length) {
    row.push(value);
    rows.push(row);
  }
  return rows;
}

const csv = await fs.readFile(csvPath, "utf8");
const rows = parseCsv(csv.replace(/^\uFEFF/, ""));
const headers = rows[0];
const data = rows.slice(1);

const workbook = Workbook.create();
const dataSheet = workbook.worksheets.add("Import_Data");
const dictionarySheet = workbook.worksheets.add("Field_Map");
const notesSheet = workbook.worksheets.add("Import_Notes");

dataSheet.getRangeByIndexes(0, 0, rows.length, headers.length).values = rows;
dataSheet.freezePanes.freezeRows(1);
dataSheet.getRangeByIndexes(0, 0, 1, headers.length).format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
};
dataSheet.getRangeByIndexes(0, 0, rows.length, headers.length).format.borders = {
  preset: "all",
  style: "thin",
  color: "#D9E2F3",
};
dataSheet.tables.add(
  dataSheet.getRangeByIndexes(0, 0, rows.length, headers.length).address,
  true,
  "GasketImportSample"
);

const widths = {
  source_name: 135,
  source_url: 360,
  source_product_id: 130,
  raw_title: 220,
  part_name: 220,
  brand_raw: 120,
  canonical_brand: 140,
  oem_part_number: 145,
  normalized_part_number: 165,
  alternate_part_numbers: 180,
  compatible_brands: 220,
  compatible_models: 180,
  door_position_raw: 155,
  door_position_normalized: 170,
  width_in: 80,
  height_in: 80,
  dimensions_text: 140,
  color: 90,
  material: 180,
  profile_type: 130,
  mounting_method: 160,
  magnetic: 90,
  temperature_application: 165,
  price_usd: 90,
  stock_status: 115,
  image_url: 320,
  description: 520,
  evidence_summary: 300,
  confidence_score: 120,
  data_status: 110,
  scraped_at: 210,
  target_table_hint: 300,
};

headers.forEach((header, index) => {
  dataSheet.getRangeByIndexes(0, index, rows.length, 1).format.columnWidthPx = widths[header] || 140;
});
dataSheet.getRangeByIndexes(1, headers.indexOf("price_usd"), data.length, 1).format.numberFormat = "$#,##0.00";
dataSheet.getRangeByIndexes(1, headers.indexOf("confidence_score"), data.length, 1).format.numberFormat = "0";

const fieldMap = [
  ["column_name", "target_table", "target_field", "import_note"],
  ["source_name", "competitor_sources / evidence", "source_name", "来源网站名称。"],
  ["source_url", "gasket_catalog / refrigerator_product_gaskets", "source_url", "原始产品页 URL，用于证据追溯。"],
  ["source_product_id", "competitor_products_raw", "source_product_id", "竞品站内部商品 ID 或 AP 编号。"],
  ["raw_title", "competitor_products_raw", "raw_title", "原始标题，保留不清洗版本。"],
  ["part_name", "gasket_catalog", "part_name", "密封条名称。"],
  ["brand_raw", "brand_aliases", "alias_name", "抓到的品牌写法。"],
  ["canonical_brand", "brand_aliases / gasket_catalog", "canonical_brand / brand", "标准品牌名。"],
  ["oem_part_number", "gasket_catalog", "primary_part_number", "优先作为标准 OEM 配件号。"],
  ["normalized_part_number", "gasket_catalog / part_number_aliases", "normalized_primary_part_number / alias_normalized", "去掉符号后的配件号，用于匹配。"],
  ["alternate_part_numbers", "part_number_aliases", "alias_part_number", "替代号，当前样本多为空。"],
  ["compatible_brands", "gasket_catalog", "compatible_brands", "从描述推断的兼容品牌。"],
  ["compatible_models", "refrigerator_product_gaskets", "compatible_models", "产品型号应用，当前列表页多为空，后续详情页/其他站补。"],
  ["door_position_raw", "refrigerator_product_gaskets", "door_position_display", "原始门位文字。"],
  ["door_position_normalized", "refrigerator_product_gaskets", "door_position", "标准门位。"],
  ["width_in", "gasket_catalog / refrigerator_product_gaskets", "width_in", "英寸宽度；从描述中能推断才填写。"],
  ["height_in", "gasket_catalog / refrigerator_product_gaskets", "height_in", "英寸高度；从描述中能推断才填写。"],
  ["dimensions_text", "gasket_catalog", "dimensions_text", "原始尺寸文字。"],
  ["color", "gasket_catalog", "color", "颜色。"],
  ["material", "gasket_catalog", "material", "材质推断。"],
  ["profile_type", "gasket_catalog", "profile_type", "横截面类型，列表页通常缺失。"],
  ["mounting_method", "gasket_catalog", "mounting_method", "安装方式推断。"],
  ["magnetic", "gasket_catalog", "magnetic", "是否磁性。"],
  ["temperature_application", "gasket_catalog", "temperature_application", "refrigerator/freezer 推断。"],
  ["price_usd", "competitor_price_history", "price", "竞品价格，建议进价格历史表。"],
  ["stock_status", "competitor_price_history", "stock_status", "库存状态。"],
  ["image_url", "gasket_catalog", "gasket_image_url", "商品图片。"],
  ["description", "competitor_products_raw / evidence", "description", "公开页面描述摘要。"],
  ["evidence_summary", "gasket_catalog", "evidence_summary", "证据说明。"],
  ["confidence_score", "gasket_catalog", "confidence_score", "初始置信度，候选数据默认 70。"],
  ["data_status", "gasket_catalog", "data_status", "candidate/verified/rejected。"],
  ["scraped_at", "competitor_products_raw", "scraped_at", "抓取时间。"],
  ["target_table_hint", "import staging", "target_table_hint", "建议导入目标。"],
];

dictionarySheet.getRangeByIndexes(0, 0, fieldMap.length, 4).values = fieldMap;
dictionarySheet.freezePanes.freezeRows(1);
dictionarySheet.getRange("A1:D1").format = {
  fill: "#375623",
  font: { bold: true, color: "#FFFFFF" },
};
dictionarySheet.getRangeByIndexes(0, 0, fieldMap.length, 4).format.borders = {
  preset: "all",
  style: "thin",
  color: "#E2F0D9",
};
dictionarySheet.getRange("A:A").format.columnWidthPx = 210;
dictionarySheet.getRange("B:B").format.columnWidthPx = 260;
dictionarySheet.getRange("C:C").format.columnWidthPx = 260;
dictionarySheet.getRange("D:D").format.columnWidthPx = 520;
dictionarySheet.getRange("D:D").format.wrapText = true;

const notes = [
  ["Item", "Value"],
  ["Rows", data.length],
  ["Source", "AppliancePartsPros public refrigerator gasket/seal category pages"],
  ["Scope", "First 100 unique gasket/seal products by normalized OEM part number"],
  ["Recommended import flow", "Load into staging table first, then upsert brand_aliases, gasket_catalog, part_number_aliases, and price history."],
  ["Verification status", "All rows are candidate data. Cross-check with at least one more source before marking verified."],
  ["Missing fields", "Compatible models and profile_type are often not present on listing pages; detail pages or vertical gasket sites should enrich these later."],
];
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).values = notes;
notesSheet.getRange("A1:B1").format = {
  fill: "#7F6000",
  font: { bold: true, color: "#FFFFFF" },
};
notesSheet.getRange("A:A").format.columnWidthPx = 190;
notesSheet.getRange("B:B").format.columnWidthPx = 760;
notesSheet.getRange("B:B").format.wrapText = true;
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).format.borders = {
  preset: "all",
  style: "thin",
  color: "#FFF2CC",
};

const preview = await workbook.render({
  sheetName: "Import_Data",
  range: "A1:J20",
  scale: 1,
  format: "png",
});
await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(path.join(outputDir, "gasket_import_sample_100_preview.png"), new Uint8Array(await preview.arrayBuffer()));

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
