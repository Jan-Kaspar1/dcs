"use strict";

// Source contract checks for shipped assets and page functions. This uses no
// browser, layout engine, network or mirrored implementation of UI semantics.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const root = path.resolve(__dirname, "../..");
const assets = path.join(root, "crates/dcs-monitor/src");
const page = fs.readFileSync(path.join(assets, "page.html"), "utf8");
const command = { write_value: { point: 24001, kind: "float", value: { float: 30 } } };

function receiptText(origin, seq, outcome = { applied: { tick: 42 } }) {
  return '{"command":' + JSON.stringify(command) + ',"actor":"operator-A",' +
    '"reason":"manual discharge","outcome":' + JSON.stringify(outcome) +
    ',"submission":{"origin":' + origin + ',"seq":' + seq + '}}';
}

function shippedContext() {
  const context = vm.createContext({});
  for (const asset of ["vendor/lossless-json.js", "wire-json.js"]) {
    vm.runInContext(fs.readFileSync(path.join(assets, asset), "utf8"), context, { filename: asset });
  }
  return context;
}

function pageFunction(name) {
  // The production page keeps top-level declarations at column zero. Read the
  // declaration itself; changes or removal fail this test instead of falling
  // back to a reimplementation.
  const expression = new RegExp("(?:^|\\n)(?:async )?function " + name + "\\([^\\n]*\\) \\{[\\s\\S]*?\\n\\}");
  const match = page.match(expression);
  assert.ok(match, "production page function is missing: " + name);
  return match[0];
}

function operatorContext() {
  const context = shippedContext();
  const notices = new Map();
  Object.assign(context, {
    journalEntries: [], pendingCommands: [], PENDING_LIMIT: 8,
    operator: "operator-A", equipmentCommandReceipt: null, alarmCommandReceipt: null,
    index: { points: [{ point: 24001, name: "LV-201 requested position" }] },
    document: { getElementById(id) {
      if (!notices.has(id)) notices.set(id, { textContent: "" });
      return notices.get(id);
    } },
  });
  for (const name of ["formatValue", "pointName", "describeCommand", "sameJson", "sameCommand",
    "sameSubmission", "abandonedSubmission", "settledCommandReceipt", "alarmCommandMessage",
    "answeredRefusal", "postCommand",
    "renderEquipmentCommandStatus", "renderAlarmCommandStatus"]) {
    vm.runInContext(pageFunction(name), context, { filename: "page.html:" + name });
  }
  return { context, notices };
}

test("vendored parser and license match their reviewed identities and preserve the served notice", () => {
  const bundle = fs.readFileSync(path.join(assets, "vendor/lossless-json.js"));
  const license = fs.readFileSync(path.join(assets, "vendor/lossless-json.LICENSE.md"));
  assert.equal(crypto.createHash("sha256").update(bundle).digest("hex"),
    "9f2972292943cc6f38c4427832147368e8887c84c88ad642e2143a9f67bb61c5");
  assert.equal(crypto.createHash("sha256").update(license).digest("hex"),
    "c5075e9ebeac2efabaddeb7ff8f215ef180f76da7837a79e3107868271693114");
  assert.ok(bundle.toString("utf8").includes(license.toString("utf8")));
});

test("adjacent u64 origins above 2^53 survive wire parsing and cannot match", () => {
  const { DcsWireJSON: wire } = shippedContext();
  const leftText = receiptText("9007199254740992", "5");
  const rightText = receiptText("9007199254740993", "5");
  assert.equal(JSON.parse(leftText).submission.origin, JSON.parse(rightText).submission.origin,
    "fixture must demonstrate the native Number collision");
  const left = wire.parse(leftText);
  const right = wire.parse(rightText);
  assert.equal(left.submission.origin, "9007199254740992");
  assert.equal(right.submission.origin, "9007199254740993");
  assert.equal(wire.sameSubmissionId(left.submission, right.submission), false);
  assert.equal(wire.sameSubmissionId(left.submission, wire.parse(leftText).submission), true);
  const incidentA = receiptText("17307262437741370921", "0");
  const incidentB = receiptText("17307262437741370922", "0");
  assert.equal(JSON.parse(incidentA).submission.origin, JSON.parse(incidentB).submission.origin);
  assert.equal(wire.sameSubmissionId(wire.parse(incidentA).submission, wire.parse(incidentB).submission), false);
});

test("large sequence numbers and maximum u64 retain distinct exact identities", () => {
  const { DcsWireJSON: wire } = shippedContext();
  for (const [left, right] of [
    ["9007199254740992", "9007199254740993"],
    ["18446744073709551614", "18446744073709551615"],
  ]) {
    const a = wire.parse(receiptText("18446744073709551615", left));
    const b = wire.parse(receiptText("18446744073709551615", right));
    assert.equal(a.submission.origin, "18446744073709551615");
    assert.equal(a.submission.seq, left);
    assert.equal(b.submission.seq, right);
    assert.equal(wire.sameSubmissionId(a.submission, b.submission), false);
    assert.equal(typeof a.command.write_value.value.float, "number");
  }
});

test("legacy safe integers are comparable; unsafe or malformed identities never establish a match", () => {
  const { DcsWireJSON: wire } = shippedContext();
  assert.equal(wire.sameSubmissionId({ origin: "0", seq: "42" }, { origin: 0, seq: 42 }), true);
  for (const origin of [9007199254740992, NaN, Infinity, -1, 1.5, "01", "1e0", "1.0", "+1", "-1", "", "18446744073709551616", null]) {
    const invalid = { origin, seq: "1" };
    assert.equal(wire.sameSubmissionId(invalid, invalid), false, "invalid origin: " + String(origin));
    assert.equal(wire.sameSubmissionId({ origin: "1", seq: origin }, { origin: "1", seq: origin }), false,
      "invalid sequence: " + String(origin));
  }
  assert.equal(wire.sameSubmissionId(null, { origin: "1", seq: "1" }), false);
  assert.equal(wire.sameSubmissionId({ origin: "1" }, { origin: "1" }), false);
});

test("ordinary measurements, literals, numeric strings and escaped keys keep their declared shapes", () => {
  const { DcsWireJSON: wire } = shippedContext();
  const ordinary = { value: { float: 2.8 }, int: -4, flag: true, missing: null,
    label: 'text "submission": {"origin":18446744073709551615}',
    numericString: "18446744073709551615", nested: [{ n: 1e-3 }, false] };
  const parsed = wire.parse(JSON.stringify(ordinary));
  assert.deepEqual(JSON.parse(JSON.stringify(parsed)), ordinary);
  assert.equal(typeof parsed.value.float, "number");
  assert.equal(typeof parsed.int, "number");
  const escaped = receiptText("18446744073709551615", "9007199254740993")
    .replace('"submission"', '"submi\\u0073sion"')
    .replace('"origin"', '"\\u006frigin"')
    .replace('"seq"', '"\\u0073eq"');
  const receipt = wire.parse(escaped);
  assert.equal(receipt.submission.origin, "18446744073709551615");
  assert.equal(receipt.submission.seq, "9007199254740993");
});

test("malformed JSON, duplicate keys and invalid receipt identities are refused", () => {
  const { DcsWireJSON: wire } = shippedContext();
  for (const text of ['{"x":', '{"x":01}', '{"x":NaN}', '{"x":1,"x":2}', '{"x":"unterminated}']) {
    assert.throws(() => wire.parse(text), "malformed document: " + text);
  }
  for (const origin of ["-1", "1.5", "1e3", "18446744073709551616"]) {
    assert.throws(() => wire.parse(receiptText(origin, "1")), /submission identity/);
  }
});

test("response parsing consumes wire text before any native numeric rounding", async () => {
  const { DcsWireJSON: wire } = shippedContext();
  let reads = 0;
  const receipt = await wire.response({
    async text() { reads++; return receiptText("18446744073709551615", "9007199254740993"); },
    async json() { throw new Error("native response.json would lose identity"); },
  });
  assert.equal(reads, 1);
  assert.equal(receipt.submission.origin, "18446744073709551615");
  assert.equal(receipt.submission.seq, "9007199254740993");
});

test("actual postCommand keeps exact admitted receipts and distinguishes answered refusals from unknown outcomes", async () => {
  const { context } = operatorContext();
  const admitted = receiptText("17307262437741370921", "9007199254740993", { accepted: { apply_tick: 42 } });
  const requests = [];
  function answer(status, text) {
    context.pollFetch = async (url, options) => {
      requests.push({ url, options });
      return { ok: status === 200, status, async text() { return text; },
        async json() { throw new Error("native decoding must never read command identities"); } };
    };
  }
  answer(200, admitted);
  assert.equal((await context.postCommand("http://controller", command, "manual discharge")).submission.origin,
    "17307262437741370921");
  assert.equal(JSON.parse(requests[0].options.body).command.write_value.value.float, 30);
  answer(500, '{"error":"state failure","durability":"unattested","receipt":' + admitted + '}');
  await assert.rejects(context.postCommand("http://controller", command, "manual discharge"), error => {
    assert.equal(error.admittedReceipt.submission.origin, "17307262437741370921");
    assert.equal(error.admittedReceipt.submission.seq, "9007199254740993");
    assert.equal(error.durability, "unattested");
    assert.equal(error.answered, undefined);
    return true;
  });
  answer(503, '{"error":"no admission response"}');
  await assert.rejects(context.postCommand("http://controller", command, null), error => {
    assert.equal(error.admittedReceipt, undefined);
    assert.equal(error.answered, undefined);
    return true;
  });
  answer(413, "request too large");
  await assert.rejects(context.postCommand("http://controller", command, null), error => error.answered === true);
  assert.equal(requests.length, 4, "each deliberate submission is sent once, with no automatic retry");
});

test("nested journal and admission-error receipt identities survive the complete transport tree", () => {
  const { DcsWireJSON: wire } = shippedContext();
  const admitted = receiptText("18446744073709551615", "9007199254740993", { accepted: { apply_tick: 42 } });
  const journal = wire.parse('[{"tick":42,"event":{"command_settled":{"receipt":' +
    receiptText("18446744073709551615", "9007199254740993") + '}}}]');
  const response = wire.parse('{"admitted_receipt":' + admitted + ',"durability":"unconfirmed"}');
  const receipt = journal[0].event.command_settled.receipt;
  assert.equal(typeof journal[0].tick, "number");
  assert.equal(wire.sameSubmissionId(receipt.submission, response.admitted_receipt.submission), true);
  assert.equal(receipt.submission.origin, "18446744073709551615");
  assert.equal(receipt.submission.seq, "9007199254740993");
});

test("ordinary maps named command, outcome and submission are not mistaken for receipts", () => {
  const { DcsWireJSON: wire } = shippedContext();
  for (const map of [
    { command: { float: 3.5 }, outcome: { bool: true }, submission: { int: 4 } },
    { command: { customer_tag: "pump" }, outcome: { float: 3.5 }, submission: 42 },
    { command: { write_value: { point: 1 } }, outcome: { customer_state: true }, submission: { float: 5 } },
  ]) {
    const parsed = wire.parse(JSON.stringify(map));
    assert.deepEqual(JSON.parse(JSON.stringify(parsed)), map);
  }
});

test("__proto__ stays an own data property for nested, escaped, null and numeric JSON values", () => {
  const { DcsWireJSON: wire } = shippedContext();
  for (const value of ['{"polluted":true}', "null", "3.5", '"ordinary data"', "false"]) {
    for (const key of ['"__proto__"', '"\\u005f\\u005fproto\\u005f\\u005f"']) {
      const parsed = wire.parse('{"nested":{' + key + ':' + value + '},' + key + ':' + value + '}');
      for (const object of [parsed, parsed.nested]) {
        assert.equal(Object.prototype.hasOwnProperty.call(object, "__proto__"), true);
        assert.equal(Object.getPrototypeOf(object).polluted, undefined);
        assert.deepEqual(JSON.parse(JSON.stringify(object.__proto__)), JSON.parse(value));
      }
      assert.equal(Object.prototype.polluted, undefined);
    }
  }
});

test("actual settledCommandReceipt ignores a colliding foreign receipt and finds its exact settlement", () => {
  const { context, notices } = operatorContext();
  const accepted = context.DcsWireJSON.parse(receiptText("9007199254740992", "7", { accepted: { apply_tick: 42 } }));
  const foreign = context.DcsWireJSON.parse(receiptText("9007199254740993", "7"));
  context.journalEntries = [{ event: { command_settled: { receipt: foreign } } }];
  assert.equal(context.settledCommandReceipt(accepted), accepted);
  context.equipmentCommandReceipt = accepted;
  context.renderEquipmentCommandStatus();
  assert.match(notices.get("equipment-receipt").textContent, /accepted.*awaiting/);
  const settled = context.DcsWireJSON.parse(receiptText("9007199254740992", "7"));
  context.journalEntries.push({ event: { command_settled: { receipt: settled } } });
  assert.equal(context.settledCommandReceipt(accepted), settled);
  context.renderEquipmentCommandStatus();
  assert.equal(notices.get("equipment-receipt").textContent, "Request applied by the controller.");
});

test("actual settledCommandReceipt also refuses adjacent large sequence collisions", () => {
  const { context } = operatorContext();
  const accepted = context.DcsWireJSON.parse(receiptText("18446744073709551615", "9007199254740992", { accepted: { apply_tick: 42 } }));
  const foreign = context.DcsWireJSON.parse(receiptText("18446744073709551615", "9007199254740993"));
  context.journalEntries = [{ event: { command_settled: { receipt: foreign } } }];
  assert.equal(context.settledCommandReceipt(accepted), accepted);
  const settled = context.DcsWireJSON.parse(receiptText("18446744073709551615", "9007199254740992"));
  context.journalEntries.push({ event: { command_settled: { receipt: settled } } });
  assert.equal(context.settledCommandReceipt(accepted), settled);
});

test("actual sameSubmission checks command, actor and reason beside exact identity", () => {
  const { context } = operatorContext();
  const receipt = context.DcsWireJSON.parse(receiptText("18446744073709551615", "9007199254740993"));
  const pending = { command: receipt.command, actor: receipt.actor, reason: receipt.reason, submission: receipt.submission };
  assert.equal(context.sameSubmission(receipt, pending), true);
  assert.equal(context.sameSubmission(receipt, { ...pending, actor: "another console" }), false);
  assert.equal(context.sameSubmission(receipt, { ...pending, reason: "different reason" }), false);
  assert.equal(context.sameSubmission(receipt, { ...pending, command: { write_value: { point: 24001, kind: "float", value: { float: 31 } } } }), false);
  assert.equal(context.sameSubmission(receipt, { ...pending, submission: null }), false);
});

test("a known admitted-but-uncertain request settles only by its exact receipt in equipment and alarm panels", () => {
  const { context, notices } = operatorContext();
  const admitted = context.DcsWireJSON.parse(receiptText("18446744073709551614", "9007199254740993", { accepted: { apply_tick: 42 } }));
  const failure = new Error("persistence failed after admission");
  failure.admittedReceipt = admitted;
  failure.durability = "unconfirmed";
  const uncertain = context.abandonedSubmission(admitted.command, admitted.reason, failure);
  const pending = context.pendingCommands[0];
  assert.equal(pending.submission.origin, "18446744073709551614");
  assert.equal(pending.submission.seq, "9007199254740993");
  const foreign = context.DcsWireJSON.parse(receiptText("18446744073709551615", "9007199254740993"));
  context.journalEntries = [{ event: { command_settled: { receipt: foreign } } }];
  assert.equal(context.sameSubmission(foreign, pending), false);
  assert.equal(context.settledCommandReceipt(uncertain), uncertain);
  const settled = context.DcsWireJSON.parse(receiptText("18446744073709551614", "9007199254740993"));
  context.journalEntries.push({ event: { command_settled: { receipt: settled } } });
  assert.equal(context.sameSubmission(settled, pending), true);
  assert.equal(context.settledCommandReceipt(uncertain), settled);
  context.equipmentCommandReceipt = uncertain;
  context.alarmCommandReceipt = uncertain;
  context.renderEquipmentCommandStatus();
  context.renderAlarmCommandStatus();
  assert.equal(notices.get("equipment-receipt").textContent, "Request applied by the controller.");
  assert.equal(notices.get("alarm-command-status").textContent, "Request applied by the controller.");
});

test("an unknown no-ID submission cannot be automatically reconciled by an identical journal command", () => {
  const { context, notices } = operatorContext();
  const unknown = context.abandonedSubmission(command, "manual discharge", new Error("response disconnected"));
  const pending = context.pendingCommands[0];
  const receipt = context.DcsWireJSON.parse(receiptText("42", "1"));
  context.journalEntries = [{ event: { command_settled: { receipt } } }];
  assert.equal(pending.submission, null);
  assert.equal(context.sameSubmission(receipt, pending), false);
  assert.equal(context.settledCommandReceipt(unknown), unknown);
  assert.match(notices.get("receipt").textContent, /No admission identity/);
  assert.match(notices.get("receipt").textContent, /resubmitting risks applying the command twice/);
});

test("served page loads the production parser before wire adapter and UI code", () => {
  const parser = page.indexOf('<script src="/lossless-json.js">');
  const adapter = page.indexOf('<script src="/wire-json.js">');
  assert.ok(parser >= 0 && adapter > parser);
  const inline = page.indexOf("<script>", adapter);
  assert.ok(inline > adapter);
});
