"use strict";
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const baseline = fs.readFileSync(path.join(root, "tests/ui.test.cjs"), "utf8");
const bootstrap = baseline.slice(0, baseline.indexOf("(async()=>{"));
const checks = `
return (async () => {
  try {
    w.dispatchEvent(new w.Event("pywebviewready"));
    await tick();
    const label = document.getElementById("contextUsage");
    w.showContextUsage({input:12450, capacity:32768, reserved:4096, free:15966, method:"estimate"});
    assert.equal(label.hidden, false);
    assert.match(label.textContent, /12,450.*32,768.*38%/);
    assert.match(label.textContent, /Estimated/);
    assert.match(label.title, /4,096/);
    await w.showSettings();
    assert.equal(document.getElementById("minResponseTokens").value, "1024");
    assert.equal(document.getElementById("contextThreshold").value, "80");
    document.getElementById("minResponseTokens").value = "1536";
    document.getElementById("contextThreshold").value = "75";
    await w.saveSettings();
    const saved = calls.find(row => row[0] === "saveSettings")[1];
    assert.equal(saved.min_response_tokens, 1536);
    assert.equal(saved.context_threshold, 75);
    await w.loadSession("demo");
    const overview = await api.get_task_overview("demo");
    api.get_task_overview = async () => ({...overview, task: {
      state: "partial", goal: "Create two files", operations: [
        {tool:"write_file", target:"a.py", state:"completed"},
        {tool:"write_file", target:"b.py", state:"failed"},
      ],
    }});
    await w.showTaskCenter();
    const progress = document.getElementById("taskProgress").textContent;
    assert.match(progress, /a.py.*Verified/);
    assert.match(progress, /b.py.*Failed/);
    assert.equal(document.getElementById("resumeTask").disabled, false);
    w.showContextUsage(null);
    assert.equal(label.hidden, true);
    console.log("Context indicator and persistent settings checks passed.");
  } finally {
    await tick();
    dispose();
  }
})()
`;
new Function("require", "__dirname", bootstrap + checks)(require, path.join(root, "tests"))
  .catch(error => { console.error(error); process.exitCode = 1; });
