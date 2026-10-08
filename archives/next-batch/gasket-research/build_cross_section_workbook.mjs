import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const cwd = process.cwd();
const csvPath = path.join(cwd, "work", "cross_section_images_100.csv");
const outputDir = path.join(cwd, "outputs");
const outputPath = path.join(outputDir, "commercial_gasket_cross_section_images_100.xlsx");

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

const rows = parseCsv((await fs.readFile(csvPath, "utf8")).replace(/^\uFEFF/, ""));
const headers = rows[0];
const data = rows.slice(1);

const workbook = Workbook.create();
const dataSheet = workbook.worksheets.add("Cross_Section_Images");
const mapSheet = workbook.worksheets.add("Field_Map");
const notesSheet = workbook.worksheets.add("Notes");

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
dataSheet.tables.add(dataSheet.getRangeByIndexes(0, 0, rows.length, headers.length).address, true, "CrossSectionImages");

const widthByHeader = {
  source_site: 120,
  source_category_url: 310,
  source_product_url: 320,
  source_product_id: 120,
  profile_code: 105,
  product_title: 220,
  cross_section_image_url: 430,
  cross_section_image_formula: 360,
  product_image_url: 350,
  image_type: 190,
  color: 90,
  magnet_code: 120,
  compatible_manufacturers: 380,
  compatible_part_numbers: 230,
  gasket_size_text: 360,
  size_source_note: 520,
  material: 230,
  mounting_method: 140,
  temperature_application: 260,
  description: 540,
  evidence_summary: 360,
  confidence_score: 120,
  data_status: 110,
  scraped_at: 210,
  target_table_hint: 330,
};
headers.forEach((header, index) => {
  dataSheet.getRangeByIndexes(0, index, rows.length, 1).format.columnWidthPx = widthByHeader[header] || 150;
});
["description", "size_source_note", "compatible_manufacturers", "gasket_size_text"].forEach((header) => {
  const idx = headers.indexOf(header);
  if (idx >= 0) dataSheet.getRangeByIndexes(0, idx, rows.length, 1).format.wrapText = true;
});
dataSheet.getRangeByIndexes(1, headers.indexOf("confidence_score"), data.length, 1).format.numberFormat = "0";

const fieldMap = [
  ["column_name", "target_table", "target_field", "import_note"],
  ["profile_code", "gasket_catalog", "gasket_code / profile_type / profile_name", "GU 横截面代码，是合并密封条本体库的关键字段。"],
  ["cross_section_image_url", "gasket_catalog", "profile_image_url / profile_drawing_url", "横截面 CAD/profile 图的原始 URL。"],
  ["cross_section_image_formula", "Google Sheets only", "preview", "导入 Google Sheets 后可显示图片预览。"],
  ["source_product_url", "evidence/source table", "source_url", "原始产品页面，必须保留用于追溯。"],
  ["color", "gasket_catalog", "color", "网页文字颜色；缺失的保留为空。"],
  ["magnet_code", "gasket_catalog / part_number_aliases", "magnet / compatible_oem_part_numbers", "页面 Magnet 字段。"],
  ["compatible_manufacturers", "gasket_catalog", "compatible_brands", "页面 Commonly fits Manufacturers 字段。"],
  ["compatible_part_numbers", "part_number_aliases", "alias_part_number", "页面 Compatible With 字段里的兼容号。"],
  ["gasket_size_text", "refrigerator_product_gaskets", "dimensions_text", "这些是 custom door gasket，成品尺寸按门封外沿宽高下单。"],
  ["size_source_note", "evidence/source table", "notes", "很多横截面具体尺寸画在图片中，不是网页文字。"],
  ["material", "gasket_catalog", "material", "PVC/Santoprene 等材质推断。"],
  ["mounting_method", "gasket_catalog", "mounting_method", "push-in/screw-in 等安装方式，能推断才填。"],
  ["data_status", "all", "data_status", "当前全部为 candidate，二次来源验证后再转 verified。"],
];
mapSheet.getRangeByIndexes(0, 0, fieldMap.length, 4).values = fieldMap;
mapSheet.freezePanes.freezeRows(1);
mapSheet.getRange("A1:D1").format = { fill: "#375623", font: { bold: true, color: "#FFFFFF" } };
mapSheet.getRangeByIndexes(0, 0, fieldMap.length, 4).format.borders = { preset: "all", style: "thin", color: "#E2F0D9" };
mapSheet.getRange("A:A").format.columnWidthPx = 230;
mapSheet.getRange("B:B").format.columnWidthPx = 260;
mapSheet.getRange("C:C").format.columnWidthPx = 290;
mapSheet.getRange("D:D").format.columnWidthPx = 620;
mapSheet.getRange("D:D").format.wrapText = true;

const notes = [
  ["Item", "Value"],
  ["Rows", data.length],
  ["Primary source", "Gasket Guy public commercial cooler gasket product pages"],
  ["What was collected", "100 commercial gasket profile/cross-section image URLs with source page, profile code, color, magnet code, compatible manufacturers, and size notes."],
  ["Dimension limitation", "Most product pages do not publish finished gasket W x H as fixed data because these are custom door gaskets; the buyer orders by measured outside-edge width and height. Some cross-section dimensions are present inside the CAD/profile images."],
  ["Image handling", "The workbook stores original image URLs plus a Google Sheets IMAGE formula column; it does not copy/rehost the website images."],
  ["Recommended next step", "Use this as a staging import for gasket_catalog.profile_image_url/profile_drawing_url, then enrich profile dimensions by OCR/manual review of the CAD images."],
];
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).values = notes;
notesSheet.getRange("A1:B1").format = { fill: "#7F6000", font: { bold: true, color: "#FFFFFF" } };
notesSheet.getRange("A:A").format.columnWidthPx = 190;
notesSheet.getRange("B:B").format.columnWidthPx = 850;
notesSheet.getRange("B:B").format.wrapText = true;
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).format.borders = { preset: "all", style: "thin", color: "#FFF2CC" };

await fs.mkdir(outputDir, { recursive: true });
const preview = await workbook.render({ sheetName: "Cross_Section_Images", range: "A1:M18", scale: 1, format: "png" });
await fs.writeFile(path.join(outputDir, "commercial_gasket_cross_section_images_100_preview.png"), new Uint8Array(await preview.arrayBuffer()));
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
