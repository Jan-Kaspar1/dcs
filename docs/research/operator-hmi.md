# Operator HMI presentation

Reviewed 2026-10-08 for `WW-OPS-001`–`WW-OPS-003` and `WW-ALM-001`–`WW-ALM-002`.

[ISA-101](https://www.isa.org/standards-and-publications/isa-standards/isa-standards-committees/isa101) addresses process-automation HMI design and its lifecycle. [IEC 62682:2022](https://webstore.iec.ch/en/publication/65543) and ISA-18.2 address alarm management, including presentation and operator response. [IEC 60073:2002](https://webstore.iec.ch/en/publication/587) covers indicator and actuator coding. [ISO 11064-5:2008](https://www.iso.org/standard/44691.html) covers control-centre displays and controls. [EEMUA 201, third edition](https://www.eemua.org/products/publications/digital/eemua-publication-201), is supporting control-room guidance, explicitly not a standard. Their public summaries establish scope; this review does not claim access to every normative clause or product conformity.

The public [ISA-hosted PAS High Performance HMI overview](https://www.isa.org/getmedia/06130a38-f7af-4b35-8c9c-2c34f25c1977/The-High-Performance-HMI-Overview-v2-01.pdf), pages 6 and 9–11, supports neutral backgrounds, restrained colour, progressive disclosure, and state indications combining colour with other cues. [HSE control-room guidance](https://www.hse.gov.uk/comah/sragtech/techmeascontrol.htm) supports readable consistent labels, clear control/display relationships, and visible action results and delays. These are the directly inspectable basis for this presentation policy:

- Use compact navigation, alarm rows and equipment faceplates. Remove repeated headings and descriptive boilerplate; retain equipment identity, mode, measured state and available actions.
- Draw normal equipment and piping in neutral greys. Distinguish running/open from stopped/closed with fill and explicit state text. Normal commands use neutral controls.
- Reserve prominent colour for abnormal conditions. The demo uses P1 red, P2 orange and P3 amber alongside priority text. This ordering and palette are product defaults requiring site review, not a universal standards mandate.
- Pulse only unacknowledged alarm indicators; respect reduced-motion preferences. Acknowledgment stops attention animation without clearing an active condition. Managed alarms and stale data remain explicitly marked.
- Keep the highest-priority unacknowledged alarms continuously accessible. Put response guidance, consequence and limits in the selected alarm detail; keep quality and command settlement visible.

Compactness comes from reducing repetition and spacing, rather than hiding abnormal state or shrinking essential text. This is an initial simulated operator interface; site task analysis and operator validation remain necessary for deployment claims.
