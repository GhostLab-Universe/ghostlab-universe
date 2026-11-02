# Equipment booking external example (Track B)

Synthetic, resettable demo outside core refund worlds. No live inference,
no real bookings.

## Flow

`walkthrough.py` executes register -> import -> qualify -> run ->
duplicate -> retest:

1. **register**: two developer-owned agents (`booking-naive`,
   `booking-corrected`) with version + config snapshots in a track-private
   SQLite DB.
2. **import**: export one manifest to JSON and re-import it (manifest flow).
3. **qualify**: non-mutating `get_equipment` probe via the controlled
   gateway.
4. **run**: baseline (no fault) plus faulted naive run
   (`timeout_after_commit=True`): commit succeeds, response times out.
5. **duplicate**: independent `has_duplicate()` check over
   controller-owned reservations.
6. **retest**: corrected stable-key agent under the same fault shows one
   booking and passing checks.

## Run

```sh
python examples/equipment_booking/walkthrough.py
python -m pytest src/ghostlab/targets/tests/test_registry_gateway.py \
  examples/equipment_booking/tests/test_booking.py
```

Temp DBs live under `/tmp/ghostlab-trackb-*` when `/tmp` is writable,
else the platform temp dir with the same prefix.

## Mediation boundary

Only `gateway.dispatch()` calls are allowlisted, budgeted, cancellable and
recorded. Direct `world.call()` bypasses the gateway by design in this
local demo; Track B is not a sandbox for unknown code.

## Environment-pipeline integration

`env_adapter.py` wraps this world in the shared environment adapter
protocol (`describe`/`reset`/`invoke`/`snapshot`/`evidence`/`set_fault`/
`clear_faults`/`invoke_cancellable`/`probe_calls`), so the example
registers, imports a manifest, discloses capabilities and qualifies
through `ghostlab.envs` with no edits to core code. Register with an
explicit allowlist entry for `examples.equipment_booking.`; see
`tests/test_booking_env_integration.py`, which also proves a broken
reset and broken isolation fail qualification.
