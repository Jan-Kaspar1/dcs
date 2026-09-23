# APL-inspired reusable-library completeness prompts

- **Checked:** 2026-09-23
- **Question:** Which observable behaviors from a targeted sample of the Siemens Advanced Process Library manual can prompt DCS library tests?
- **Affected requirements:** product-strategy library completeness; `WW-FND-003`, `WW-OPS-001`, `WW-OPS-002`, `WW-OPS-003`
- **Scope:** selected pages only. The 2,514-page manual was not reviewed in full. These observations are test prompts, not accepted DCS requirements.

## Sources

1. Siemens, *Advanced Process Library (V9.0)*, Siemens Industry Online Support entry 109812806: <https://support.industry.siemens.com/cs/ww/en/view/109812806>. The reviewed local copy was `C:\Users\Kaspar\Downloads\s7jal90a_de-DE.pdf`; page numbers below are PDF page numbers. The targeted sample was 41, 42, 59, 60, 71, 72, 75, 124, 234, 251, 257, 260, 347, 437, 1093, 1601, 1663, and 2369.
2. Siemens, *Plant-wide automation in the water industry*, version 4.2, 09/2024, entry 109748869: <https://support.industry.siemens.com/cs/ww/en/view/109748869>. See the existing water-library research note for its supported findings and DCS proposals.

## Observations and DCS test prompts

| Selected manual pages | Observation from the sample | DCS test prompt |
| --- | --- | --- |
| 41–42 | Override or force behavior has explicit precedence and visible status; contradictory inputs are rejected without changing actuation. | Exercise source precedence and contradictory requests. Assert a named refusal, unchanged output, and visible effective-source state. |
| 59–60 | External and internal simulation are distinct; internal simulation can emulate values and feedback for commissioning. | Keep simulation origin visible and separate from value and quality. Test that simulated data cannot silently cross into a live deployment path. |
| 71–75 | Mode choices and their priorities vary by block family. | Declare legal transitions for each DCS item. Test invalid transitions, startup, restart, and handover against the accepted operator workflow. |
| 124 | Error and signal status distinguish conditions including substituted, simulated, bad, and maintenance states. | Inject bad, stale, and substituted inputs; verify declared quality propagation, per-block response, alarm meaning, and recovery. |
| 234, 251, 257, 260 | Operator symbols and faceplates expose information and gate actions according to availability and configured permissions or mode. | Test controller unavailability, command availability, authorization refusal, and receipt behavior through the generic UI and contract. |
| 347, 437, 1093 | Block variants expose different capability sets and resource costs. | Declare any lean or full capability profile explicitly and measure its actual DCS resource cost. Do not reuse vendor figures. |
| 1601, 1663 | Interlock and event examples organize input combinations, status context, labeled events, acknowledgment, and suppression. | Test interlock truth tables, quality propagation, event identity and ordering, and the declared acknowledgment or suppression lifecycle through restart and UI loss. |
| 2369 | Insertable templates package recurring patterns, including lean variants. | Test DCS composition templates as explicit model-building conveniences with the same contract and scenarios as their expanded composition. |

## DCS interpretation

These observations reinforce the product strategy's completeness dimensions: typed contract, declared control behavior, operator availability and command feedback, diagnostics and quality, reproducible scenarios, and explicit capability variants. They do not establish the DCS mode vocabulary, security model, alarm policy, resource budget, or customer workflow.

DCS architecture decisions and accepted customer-backed requirements define DCS semantics. Do not copy vendor names, code, screen layouts, diagrams, or numerical performance claims. Record any adopted prompt against a stable DCS requirement ID and executable scenario; leave it as a research question when evidence is insufficient.

## Assumptions for validation

- Which modes, override sources, and conflict priorities belong in a given customer workflow?
- What role and availability policy should govern each command, and where should authorization be enforced?
- Which simulation states and origins must be visible during engineering and operation?
- Which diagnostics and event transitions must persist, and what acknowledgment or suppression policy applies?
- Are capability variants valuable for a measured DCS constraint, and what contract omissions would each variant declare?
