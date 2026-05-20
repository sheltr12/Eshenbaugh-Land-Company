import fs from "node:fs/promises";

const [,, targetName, outputPath] = process.argv;

if (!targetName || !outputPath) {
  console.error("Usage: node download_hcpa_file.mjs <filename> <output-path>");
  process.exit(2);
}

const baseUrl = "https://downloads.hcpafl.org/Default.aspx";

function matchField(html, name) {
  const re = new RegExp(`name="${name}"[^>]*value="([^"]*)"`);
  const match = html.match(re);
  return match ? match[1] : "";
}

const indexResponse = await fetch(baseUrl);
if (!indexResponse.ok) {
  throw new Error(`Index fetch failed: ${indexResponse.status}`);
}

const html = await indexResponse.text();
const anchorRe = /<a href="javascript:__doPostBack\(&#39;([^&]+)&#39;,&#39;&#39;\)">([\s\S]*?)<\/a>/g;
const rowMatch = [...html.matchAll(anchorRe)].find((match) => match[2].includes(targetName));

if (!rowMatch) {
  throw new Error(`Could not find ${targetName} in HCPA downloads index`);
}

const form = new URLSearchParams();
form.set("__EVENTTARGET", rowMatch[1].replaceAll("$", "$"));
form.set("__EVENTARGUMENT", "");
form.set("__VIEWSTATE", matchField(html, "__VIEWSTATE"));
form.set("__VIEWSTATEGENERATOR", matchField(html, "__VIEWSTATEGENERATOR"));
form.set("__EVENTVALIDATION", matchField(html, "__EVENTVALIDATION"));
form.set("grdFiles_ClientState", "");

const response = await fetch(baseUrl, {
  method: "POST",
  headers: {
    "content-type": "application/x-www-form-urlencoded",
    "referer": baseUrl,
  },
  body: form,
});

if (!response.ok) {
  throw new Error(`Download failed: ${response.status}`);
}

const buffer = Buffer.from(await response.arrayBuffer());
await fs.writeFile(outputPath, buffer);
console.log(`Downloaded ${targetName} via ${rowMatch[1]} (${buffer.length} bytes)`);
