// Preserve SubmissionId's u64 identity at the disposable UI boundary.
// The controller's numeric JSON and ordinary display arithmetic are unchanged.
(function (global) {
  "use strict";

  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const maximum = "18446744073709551615";
  const commandKinds = ["write_value", "force_point", "unforce_point", "set_parameter", "invoke"];
  const outcomeKinds = ["accepted", "applied", "rejected", "indeterminate"];

  function receiptShape(value) {
    const command = value.command;
    const outcome = value.outcome;
    return own(value, "command") && own(value, "outcome") &&
      command !== null && typeof command === "object" &&
      outcome !== null && typeof outcome === "object" &&
      Object.keys(command).length === 1 && commandKinds.some(kind => own(command, kind)) &&
      Object.keys(outcome).length === 1 && outcomeKinds.some(kind => own(outcome, kind));
  }

  function unsignedIdentity(value) {
    if (value instanceof global.LosslessJSON.LosslessNumber) value = value.value;
    else if (typeof value === "number") {
      // A rounded Number cannot establish which wire identity it came from.
      if (!Number.isSafeInteger(value) || value < 0) return null;
      value = String(value);
    }
    if (typeof value !== "string" || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
    if (value.length > maximum.length ||
        (value.length === maximum.length && value > maximum)) return null;
    return value;
  }

  function normalize(value) {
    if (value instanceof global.LosslessJSON.LosslessNumber) return Number(value.value);
    if (value === null || typeof value !== "object") return value;
    // Parameter/event maps may themselves have fields named command,
    // outcome and submission. Their typed Values are not command receipts.
    const receipt = receiptShape(value);
    for (const key of Object.keys(value)) {
      if (receipt && key === "submission" && value[key] !== null) {
        const submission = value[key];
        const origin = unsignedIdentity(submission?.origin);
        const seq = unsignedIdentity(submission?.seq);
        if (origin === null || seq === null) throw new SyntaxError("invalid submission identity");
        submission.origin = origin;
        submission.seq = seq;
      }
      value[key] = normalize(value[key]);
    }
    return value;
  }

  function parse(text) {
    // Use the established parser before any conversion to native Numbers.
    // Conflicting duplicate keys are refused rather than choosing an identity.
    return normalize(global.LosslessJSON.parse(text));
  }

  async function response(answer) {
    return parse(await answer.text());
  }

  function sameSubmissionId(left, right) {
    if (!left || !right) return false;
    const origin = unsignedIdentity(left.origin);
    const seq = unsignedIdentity(left.seq);
    return origin !== null && seq !== null &&
      origin === unsignedIdentity(right.origin) && seq === unsignedIdentity(right.seq);
  }

  global.DcsWireJSON = Object.freeze({ parse, response, sameSubmissionId });
})(globalThis);
