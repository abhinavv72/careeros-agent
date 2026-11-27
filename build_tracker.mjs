import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = new URL(".", import.meta.url).pathname.replace(/^\/(.:)/, "$1");
const csvPath = `${root}data/exports/JobApplications.csv`;
const outputPath = `${root}data/exports/JobApplications.xlsx`;
const previewPath = `${root}data/exports/JobApplications-preview.png`;
const applicationsPreviewPath = `${root}data/exports/JobApplications-table-preview.png`;
const csvText = await fs.readFile(csvPath, "utf8");
const workbook = await Workbook.fromCSV(csvText, { sheetName: "Applications" });
const applications = workbook.worksheets.getItem("Applications");
const dashboard = workbook.worksheets.add("Dashboard");

dashboard.showGridLines = false;
applications.showGridLines = false;
dashboard.tabColor = "#17365D";
applications.tabColor = "#5B9BD5";

dashboard.getRange("A2:F2").merge();
dashboard.getRange("A2").values = [["JobPilot dashboard"]];
dashboard.getRange("A2:F2").format.font = { name: "Arial", size: 16, bold: true, color: "#17365D" };
dashboard.getRange("A3:F3").format.borders = { bottom: { style: "thin", color: "#9FBAD0" } };
dashboard.getRange("A5:B5").values = [["Pipeline", "Count"]];
dashboard.getRange("A6:A11").values = [["All jobs"], ["Queued"], ["Manual review"], ["Skipped"], ["Applied"], ["Interview"]];
dashboard.getRange("B6:B11").formulas = [
  ["=COUNTA(Applications!A2:A5000)"],
  ["=COUNTIFS(Applications!O2:O5000,\"Queued\")"],
  ["=COUNTIFS(Applications!O2:O5000,\"ManualReview\")"],
  ["=COUNTIFS(Applications!O2:O5000,\"Skipped\")"],
  ["=COUNTIFS(Applications!O2:O5000,\"Applied\")"],
  ["=COUNTIFS(Applications!O2:O5000,\"Interview\")"],
];
dashboard.getRange("D5:E5").values = [["Priority", "Count"]];
dashboard.getRange("D6:D9").values = [["P0"], ["P1"], ["P2"], ["P3"]];
dashboard.getRange("E6:E9").formulas = [
  ["=COUNTIFS(Applications!H2:H5000,\"P0\")"], ["=COUNTIFS(Applications!H2:H5000,\"P1\")"],
  ["=COUNTIFS(Applications!H2:H5000,\"P2\")"], ["=COUNTIFS(Applications!H2:H5000,\"P3\")"],
];
dashboard.getRange("A5:B5").format = { fill: "#17365D", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
dashboard.getRange("D5:E5").format = { fill: "#17365D", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
dashboard.getRange("A6:B11").format.font = { name: "Arial", size: 10 };
dashboard.getRange("D6:E9").format.font = { name: "Arial", size: 10 };
dashboard.getRange("B6:B11").format.numberFormat = "#,##0";
dashboard.getRange("E6:E9").format.numberFormat = "#,##0";
dashboard.getRange("A13:F13").merge();
dashboard.getRange("A13").values = [["Open the Applications sheet to filter by score, priority, ATS, status, company, or location."]];
dashboard.getRange("A13:F13").format.font = { name: "Arial", size: 10, italic: true, color: "#595959" };
dashboard.getRange("A:A").format.columnWidth = 22;
dashboard.getRange("B:B").format.columnWidth = 12;
dashboard.getRange("C:C").format.columnWidth = 4;
dashboard.getRange("D:D").format.columnWidth = 18;
dashboard.getRange("E:E").format.columnWidth = 12;

const used = applications.getUsedRange();
used.format.font = { name: "Arial", size: 10 };
applications.getRange("A1:P1").format = { fill: "#17365D", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true };
applications.getRange("A1:P1").format.rowHeight = 30;
applications.freezePanes.freezeRows(1);
applications.getRange("A:A").format.columnWidth = 10;
applications.getRange("B:C").format.columnWidth = 20;
applications.getRange("D:D").format.columnWidth = 24;
applications.getRange("E:E").format.columnWidth = 32;
applications.getRange("F:F").format.columnWidth = 24;
applications.getRange("G:H").format.columnWidth = 12;
applications.getRange("I:J").format.columnWidth = 18;
applications.getRange("K:L").format.columnWidth = 42;
applications.getRange("M:P").format.columnWidth = 20;
const rowCount = Math.max(2, used.rowCount);
applications.tables.add(`A1:P${rowCount}`, true, "ApplicationsTable");
applications.getRange(`B2:C${rowCount}`).format.numberFormat = "yyyy-mm-dd hh:mm";
applications.getRange(`G2:G${rowCount}`).format.numberFormat = "0.0";

workbook.recalculate();
const preview = await workbook.render({ sheetName: "Dashboard", range: "A1:F14", scale: 2, format: "png" });
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
const applicationsPreview = await workbook.render({ sheetName: "Applications", range: "A1:P12", scale: 1, format: "png" });
await fs.writeFile(applicationsPreviewPath, new Uint8Array(await applicationsPreview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

const check = await workbook.inspect({ kind: "table", range: "Dashboard!A1:F14", include: "values,formulas", tableMaxRows: 14, tableMaxCols: 6 });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
console.log(check.ndjson);
console.log(errors.ndjson);
console.log(outputPath);
