# Connected water candidate delivery

The connected-water acceptance proof can produce a versioned, self-contained
delivery archive for an independent engineering machine. It contains the exact
candidate and customer Git bundles, original Cargo manifest and lock identity,
locked SDK and registry sources, model/dynamics/scenario documents, and the exact
generic controller, model checker, simulated field service and engineering
executables used by the proof. A SHA-256 manifest binds every file to that set.
The package is a release candidate for simulated I/O. It is not a published tag,
container image, signed provenance statement or DCS activation transaction.

## Produce the archive

The platform's gated `water-area-proof` first records immutable artifact evidence
in `target/water-area-evidence/customer.json`. It builds engineering and runtime
tools from the same candidate commit. Export those existing artifacts rather
than substituting the current workspace's binaries:

```sh
python3 scripts/water_artifact_bundle.py export \
  --evidence target/water-area-evidence/customer.json \
  --tools target/water-candidate/<candidate-commit>/debug \
  --engineering target/water-candidate/<candidate-commit>/customer/debug/connected-water \
  --output target/water-area-evidence/connected-water-<candidate-tag>-<candidate-commit>-linux-x86_64.tar.gz
```

Export refuses a binary, document or source bundle whose hash differs from the
proof. Cargo vendoring copies the locked dependency sources; it does not rebuild
them. Registry access may be required at export if the sources are not cached.
Archive ordering, timestamps, ownership and gzip metadata are fixed, so exporting
the same inputs produces identical bytes. Record the archive's printed SHA-256
through the delivery channel used for that customer.

## Restore on an independent machine

The prebuilt executables require Linux x86_64 with the system ELF loader, libc,
libm and libgcc_s. Python 3.11 or newer and Git restore the archive. Rust
1.98.1 and Cargo are required for `check` (which resolves frozen SDK metadata)
and for customer compilation; the toolchain itself is not included. Install it
before going offline. Running the prebuilt controller and field service does
not require Cargo.

Copy the archive and this small delivery helper to the engineering machine.
Compare the archive SHA-256 with the recorded delivery identity. Restore into a
new directory, outside any DCS source checkout:

```sh
python3 water_artifact_bundle.py restore connected-water-<identity>.tar.gz /opt/customer-water
python3 /opt/customer-water/delivery/water_artifact_bundle.py check /opt/customer-water
```

The original customer source is cloned into `/opt/customer-water/plant`. Its
Cargo.toml, Cargo.lock, tag and commit pins remain byte-identical to the clean
acceptance checkout. The release source is restored separately for inspection.
`plant/.cargo/config.toml` maps the original Git and registry sources to the
delivered `plant/vendor` directory. The expired temporary `file://` proof origin
need not exist: Cargo's [source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html)
uses exact copies of those sources, rather than changing their locked identity.
[cargo vendor](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html) supplies
the configuration and per-crate checksum metadata.

`check` verifies the archive files and installed vendor copies, resolves the
customer with `cargo metadata --frozen`, reproduces both two- and three-pump
models/dynamics from the delivered engineering executable, and validates,
assembles and preflights both areas using the delivered generic binaries. The
checks write `check-evidence.json`. Archive hashes detect changed bytes against
the recorded identity; they do not authenticate an untrusted producer.

## Engineer and operate

From the restored customer checkout, change `WaterAreaConfig` in
`src/bin/connected-water.rs`. The existing `--add-pump` option demonstrates a
third instance through the supported Rust API. Its standard controls, alarms,
diagnostics and operator symbol come from the same model. No customer JavaScript
or controller program is needed.

```sh
cd /opt/customer-water/plant
cargo build --frozen --features connected-water --bin connected-water
mkdir -p model state
target/debug/connected-water > model/site-water.json
target/debug/connected-water --dynamics > model/site-dynamics.json
../bin/dcs-model validate model/site-water.json
../bin/dcs-controller model/site-water.json --check
../bin/dcs-plant-server model/site-water.json --check-dynamics model/site-dynamics.json
```

Customer builds can run offline because the dependency sources are delivered.
Do not edit vendor files to change platform behavior: repin to another reviewed
immutable platform artifact, then regenerate the vendor set. Keep site process
engineering in the customer's Rust source.

In separate terminals, from the same checkout:

```sh
../bin/dcs-plant-server model/site-water.json --dynamics model/site-dynamics.json --listen 127.0.0.1:9012
```

```sh
../bin/dcs-controller model/site-water.json --remote 127.0.0.1:9012 --listen 127.0.0.1:9081 --scan-ms 200 --dt 0.2 --state-file state/controller.json --journal-file state/journal.jsonl --history-file state/history.jsonl
```

Open `http://127.0.0.1:9081/`. The [connected-area guide](../CONNECTED-WATER.md)
describes the operator actions and existing acceptance harness. Run that harness
with these bundled binary paths to gather customer-owned evidence.

The delivery archive is the engineered starting configuration, not a backup of
a running plant. Runtime checkpoint, live settings, alarm/journal/history state,
field ownership and recovery basis are not captured. Adding equipment changes
the model fingerprint and may require a cold simulation run; copying a binary
over a running controller does not establish compatible takeover. Public release
publication, container image digests, online migration, signing, role policy and
site commissioning remain separate deliverables. The plan/apply proposal defines
the questions DCS must resolve before claiming activation semantics.
