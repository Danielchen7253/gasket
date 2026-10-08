import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const cwd = process.cwd();
const csvPath = path.join(cwd, "work", "commercial_gasket_import_sample.csv");
const outputDir = path.join(cwd, "outputs");
const outputPath = path.join(outputDir, "commercial_gasket_import_sample_100.xlsx");

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
const dataSheet = workbook.worksheets.add("Commercial_Import_Data");
const mapSheet = workbook.worksheets.add("Field_Map");
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
dataSheet.tables.add(dataSheet.getRangeByIndexes(0, 0, rows.length, headers.length).address, true, "CommercialGasketImport");

const wide = new Set(["source_url", "image_url", "description", "evidence_summary", "target_table_hint"]);
headers.forEach((header, index) => {
  let width = 145;
  if (wide.has(header)) width = header === "description" ? 520 : 330;
  if (["raw_title", "part_name", "commercial_fit_manufacturers", "compatible_oem_part_numbers"].includes(header)) width = 240;
  dataSheet.getRangeByIndexes(0, index, rows.length, 1).format.columnWidthPx = width;
});
dataSheet.getRangeByIndexes(1, headers.indexOf("price_usd"), data.length, 1).format.numberFormat = "$#,##0.00";
dataSheet.getRangeByIndexes(1, headers.indexOf("confidence_score"), data.length, 1).format.numberFormat = "0";

const fieldMap = [
  ["column_name", "target_table", "target_field", "import_note"],
  ["market_category", "refrigerator_products / gasket_catalog", "market_category", "本表固定为 commercial。"],
  ["equipment_category", "refrigerator_products", "equipment_category", "商用 refrigerator/freezer/cooler 方向。"],
  ["commercial_fit_manufacturers", "gasket_catalog", "compatible_brands", "Gasket Guy 页面里的 Commonly fits Manufacturers。"],
  ["gasket_profile_code", "gasket_catalog", "profile_type / gasket_code", "GU profile，可作为密封条本体的核心识别字段。"],
  ["oem_part_number", "gasket_catalog", "primary_part_number", "这里优先用 GU profile；后续可用 OEM 号交叉验证。"],
  ["compatible_oem_part_numbers", "part_number_aliases / gasket_catalog", "alias_part_number / compatible_oem_part_numbers", "页面 Compatible With 字段，可能是 OEM 或内部兼容号。"],
  ["canonical_brand", "brand_aliases / gasket_catalog", "canonical_brand / brand", "标准品牌名，如 True、Traulsen、Beverage-Air。"],
  ["color", "gasket_catalog", "color", "标准化颜色。"],
  ["magnetic", "gasket_catalog", "magnetic", "页面 Magnet 字段存在则 yes。"],
  ["source_url", "evidence", "source_url", "原始公开来源。"],
  ["data_status", "all staging targets", "data_status", "初始均为 candidate，需二次来源验证。"],
  ["confidence_score", "gasket_catalog", "confidence_score", "商用垂直站来源，初始设 75。"],
];
mapSheet.getRangeByIndexes(0, 0, fieldMap.length, 4).values = fieldMap;
mapSheet.freezePanes.freezeRows(1);
mapSheet.getRange("A1:D1").format = { fill: "#375623", font: { bold: true, color: "#FFFFFF" } };
mapSheet.getRangeByIndexes(0, 0, fieldMap.length, 4).format.borders = { preset: "all", style: "thin", color: "#E2F0D9" };
mapSheet.getRange("A:A").format.columnWidthPx = 230;
mapSheet.getRange("B:B").format.columnWidthPx = 260;
mapSheet.getRange("C:C").format.columnWidthPx = 260;
mapSheet.getRange("D:D").format.columnWidthPx = 560;
mapSheet.getRange("D:D").format.wrapText = true;

const notes = [
  ["Item", "Value"],
  ["Rows", data.length],
  ["Source", "Gasket Guy public commercial cooler-gaskets category pages"],
  ["Scope", "100 commercial refrigeration gasket products, filtered to gasket items only"],
  ["Why this replaces the prior file", "The prior AppliancePartsPros sample was mostly residential/home appliance gasket data. This file targets commercial refrigeration first."],
  ["Price caveat", "BigCommerce option products can show placeholder prices; use price_usd as candidate price evidence only."],
  ["Recommended import flow", "Load into staging first, then upsert brand_aliases, gasket_catalog, part_number_aliases, and evidence/source tables."],
];
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).values = notes;
notesSheet.getRange("A1:B1").format = { fill: "#7F6000", font: { bold: true, color: "#FFFFFF" } };
notesSheet.getRange("A:A").format.columnWidthPx = 190;
notesSheet.getRange("B:B").format.columnWidthPx = 780;
notesSheet.getRange("B:B").format.wrapText = true;
notesSheet.getRangeByIndexes(0, 0, notes.length, 2).format.borders = { preset: "all", style: "thin", color: "#FFF2CC" };

await fs.mkdir(outputDir, { recursive: true });
const preview = await workbook.render({ sheetName: "Commercial_Import_Data", range: "A1:K20", scale: 1, format: "png" });
await fs.writeFile(path.join(outputDir, "commercial_gasket_import_sample_100_preview.png"), new Uint8Array(await preview.arrayBuffer()));

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
