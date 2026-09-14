# Architecture decisions

## Bootstrap baseline

The repository begins as a Rust workspace with a `dcs-core` library reserved for shared contracts. No controller or plant-model API has been chosen yet. Python standard-library tooling coordinates development; it is not the plant runtime.

The project vision in `AGENTS.md` is the source for product decisions. All initial I/O is simulated, and development excludes physical plant equipment and live deployment.

## Recording decisions

When choosing a shared contract, module boundary, persistence format, or runtime behavior, the planner records the problem, chosen approach, alternatives, consequences, and affected tickets here. Changes are committed and pass the same CI gate as implementation. Accepted decisions must distinguish implemented behavior from proposed behavior.
