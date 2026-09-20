"use strict";

const { spawnSync } = require("node:child_process");

const modules = ["service_contract", "domain_contract"];
const result = spawnSync("python3", ["-m", "unittest", "-v", ...modules], { stdio: "inherit" });
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
