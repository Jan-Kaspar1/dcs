# Five operator design directions

Kaspar requested five alternatives on 2026-10-11, with a possible switch after
choosing one. These are review material. The production monitor does not load
these assets, persist a theme selection, or change its controller contract.
Water/wastewater remains the active market; automatic backlog generation stays
disabled.

| Direction | Main character | Operator tradeoff |
| --- | --- | --- |
| **01 Precision** | Flat light gray, square geometry, compact ruled controls, aligned technical values. | Dense and fast to scan; little decorative hierarchy. |
| **02 Civic** | Warm white, teal selection, soft corners, a floating inspector and generous spacing. | Approachable and calm; larger footprint. |
| **03 Drafting** | Engineering sheet, restrained grid, navy linework and monospaced headings/readings. | Strong technical identity; more visually formal. |
| **04 Editorial** | Open white canvas, serif headings, prominent values, typography and thin rules. | Distinctive and spacious; lowest density. |
| **05 Instrument** | Navy navigation rail with a light process canvas, compact instruments and structured feedback sections. | Strong separation and analytic emphasis; more surface structure. |

Every option keeps navigation, the alarm strip, the central process schematic
and the equipment inspector. Alarm priority and lifecycle, fault and degraded
quality cues remain independent of selection. Existing display bounds, units,
quality, availability reasons and requested/applied/actual distinctions continue
to come from the shared model and production renderer.

## Interactive comparison

Start the existing simulated water area first, then run:

```sh
python3 scripts/operator_style_preview.py \
  --listen 192.168.178.107:9083 \
  --upstream http://192.168.178.107:9081
```

Open `http://192.168.178.107:9083/`. Five native buttons switch CSS on the same
loaded operator page; the selected equipment and live polling remain in place.
Desktop/phone controls change the iframe viewport width. Full-screen links open
individual directions. Equipment selection, navigation and disclosure sections
use the existing monitor behavior. The initial selection opens LV-201 through
its existing model-bound symbol so the inspector is visible for comparison.

The proxy accepts only an explicit set of monitor GET reads and static assets.
Every write method and activation route is refused before contacting the
upstream. An early browser guard also blocks non-GET fetches, including the
production page's automatic alarm acknowledgment releases. This is a read-only
design review, not an alternative command path. No customer JavaScript or
controller/UI configuration is added to a plant project.

## Static studies and verification limits

`generate_studies.py` generates the five standalone SVGs and the contact sheet
from one captured `/signals` and `/snapshot`. Model-authored node positions,
bindings, pipe endpoints, units and display bounds supply their process content.
The assets record the capture time/tick and source documents for reproduction.
Controls in those illustrations send no command; trends and command outcomes
are not invented. The interactive page uses the actual renderer and live data,
so the studies are illustrations rather than screenshots or an extra UI runtime.

Reproduce the checked-in studies without a running controller:

```sh
python3 docs/design/operator-styles/generate_studies.py \
  --from-study docs/design/operator-styles/assets/precision.svg
```

This preserves the embedded timestamp, source identities and captured values;
all six outputs reproduce byte-identically. The captured study contains 19
authored nodes and 13 pipe endpoint pairs.

The SVGs were rendered with librsvg and visually inspected. The review proxy
passed 56 fake-upstream HTTP checks, including zero forwarded mutations,
identical production assets, refused redirects and the 8 MiB response bound.
Node 22 source-only checks cover permitted GETs, rejected writes/external
origins, dynamic read-only controls and blocked form submission.
`python3 scripts/verify.py
--phase ui-tests` passes the 17 existing production contract tests. Production
Rust and control sources are untouched by this design exploration.

Actual browser review remains pending: the computer-use tool currently exposes
no browser. The phone-width toggle and vector render are not substitutes for
desktop/phone, keyboard, reduced-motion or abnormal-state browser acceptance.
Choosing a direction authorizes the subsequent implementation work; this study
does not claim that work or the original connected-area milestone is complete.
