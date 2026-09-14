# Siemens process-library and water references

- **Checked:** 2026-09-14
- **Question:** Which established control-library concepts and water applications should inform the first DCS reference plant and component research?
- **Affected requirements:** `WW-OPS-001`, `WW-OPS-002`, `WW-OPS-003`, `WW-CTL-001`, `WW-CTL-002`, `WW-ENG-001`

## Primary sources

1. Siemens, *SIMATIC Process Control System PCS 7 Advanced Process Library*, product support entry 109812806. <https://support.industry.siemens.com/cs/ww/en/view/109812806>
2. Siemens, *Plant-wide automation in the water industry*, version 4.2, 09/2024, entry 109748869. <https://support.industry.siemens.com/cs/ww/en/view/109748869>
3. Siemens, *SIMATIC PCS 7 Industry Library*, product support entry 109815841. <https://support.industry.siemens.com/cs/ww/en/view/109815841>
4. Siemens, *Standard PCS 7 Water Templates for Water Applications*, document 78604785. <https://support.industry.siemens.com/cs/ww/en/view/78604785>

## Supported findings

- Siemens treats the Advanced Process Library as the general process-control library and the Industry Library as an extension for industry-specific functions. The water guide points to both, supporting a layered product model: stable general equipment modules plus water-specific assemblies and templates.
- Siemens publishes water templates and plant-wide guidance for water treatment, wastewater treatment, irrigation, desalination, and pumping applications. This supports using a representative water application to exercise the platform end to end.
- The source set connects control blocks with operator presentation, diagnostics, and engineering templates. That supports treating component logic and UI behavior as one library deliverable rather than separate feature inventories.

## Proposed DCS implications

- First validate the shared contracts with a duty/standby pumping station because it exercises analog measurement, discrete equipment, modes, permissives, interlocks, alarms, sequencing, and operator interaction in a bounded model.
- Separate reusable equipment modules from water-specific process assemblies. Keep each module's descriptors and faceplate semantics tied to the same contract as its runtime behavior.
- Use the manuals to build a comparison matrix of behaviors and failure states. Select behavior according to our requirements and architecture rather than matching Siemens names or screen layouts.

## Customer-validation questions

- Which first-client plant type and operating model should the reference station represent: lift station, treatment inlet works, clean-water booster, or another case?
- Which pump rotation, assist-start, maintenance, and failed-feedback policies are used in the client's standard specifications?
- Which alarm classes, shelving rules, reports, historian retention, user roles, and audit requirements are mandatory for the first deployment?
- What engineering artifacts must be imported, generated, reviewed, and handed over during commissioning?
- Which controller, I/O, fieldbus, network, redundancy, and update constraints exist at the intended plant?
