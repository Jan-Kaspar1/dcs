# QA lane deployment (Lenovo)

Generic deployment assets for the hardware/simulation QA lane. The
concrete, sanitized record of what is installed lives in the homelab
repository; this directory is the contract those copies mirror.

## Layout

- `/srv/homelab/dcs-hwtest/` — deployment config: a pinned copy of the
  `qa_lane/` Python module and `config.json`. The lane code is pinned
  separately from the revision under test; upgrading it is a deliberate
  deployment step, not a side effect of a merge.
- `/srv/dcs-hwtest/` — bounded NVMe run storage: `state.db`, `lock`,
  `src/<sha>.tar` + extracted source, `build-cache/`, `cargo-cache/`,
  `runs/<run_id>/` (report.json, evidence/, timeline.jsonl), and
  `reports/<run_id>.json` staged for the WSL relay.

## Install

```sh
sudo mkdir -p /srv/homelab/dcs-hwtest /srv/dcs-hwtest
sudo chown jan-kaspar:jan-kaspar /srv/homelab/dcs-hwtest /srv/dcs-hwtest
# copy qa_lane/ from a chosen commit of the dcs repo
cp -r qa_lane /srv/homelab/dcs-hwtest/
cp config.example.json /srv/homelab/dcs-hwtest/config.json
sudo cp dcs-hwtest.service dcs-hwtest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dcs-hwtest.timer
```

The service runs oneshot cycles as user `jan-kaspar` (docker group); a
flock in the state directory plus the oneshot unit type guarantee one
active run at a time.

## Operation

- WSL dispatch pushes `src/<sha>.tar` (`git archive` of the resolved
  main revision) and calls `python3 -m qa_lane enqueue <sha>`.
- Each cycle reconciles dead runs and orphaned labeled containers,
  re-queues one retry for an inconclusive SHA, enforces the daily
  budget, then runs the newest queued revision.
- `python3 -m qa_lane status` reports queue, active run, and history.

## Isolation and limits

- Rig containers run on a dedicated `--internal` bridge per run with
  monitor ports bound to 127.0.0.1 only; every run object carries the
  `dcs-hwtest.managed=1` + `dcs-hwtest.run=<id>` labels reconciliation
  uses to reap orphans.
- CPU, memory, swap, PIDs, and log bounds apply to every rig container
  and to the builder container; free space is checked before each run.
- The docker daemon's RequiresMountsFor/BindsTo dependency on the HDD
  mount is preserved untouched — QA containers inherit it like all
  others on this host.
