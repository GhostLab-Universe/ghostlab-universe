# Boundary-real booking service (Track E1)

A real client/server boundary on loopback HTTP, so fault injection exercises
genuine at-least-once delivery instead of an in-process flag.

## Layout

- `service.py` — deterministic equipment-booking service (`ThreadingHTTPServer`
  on `127.0.0.1`, ephemeral port). `POST /get_equipment`,
  `/get_booking_status`, `/book_equipment` mirror the
  `examples/equipment_booking` world tools and schemas; test-only
  `GET /state` returns the authoritative snapshot and `POST /reset` restores
  a fixture. Reservation ids are deterministic (`booking-N` per generation).
- `env_adapter.py` — `ProxyEnvAdapter`, the 9-method env protocol over
  service-plus-proxy. Tool invokes go through the proxy; `/reset` and
  `/state` go directly to the service so the transcript holds exactly one
  entry per accepted invoke. Fault hooks: `timeout_after_commit` and
  `permission_revoked`, both taking `{}` params.
- `demo.py` — runnable script (`python examples/booking_service/demo.py` from
  the repo root). Prints a JSON summary, exit 0 only when naive duplicates
  (2 bookings), stable-key repairs (1 booking), and revoke-mid-flow 403s
  with no duplicate.
- `ghostlab/proxy` — the `BoundaryProxy` itself: forwards agent HTTP to the
  real service and records a boundary transcript of
  `{seq, method, path, request, upstream_status, injected}`.

## Boundary-real fidelity basis

- **Forward-then-drop = real at-least-once.** On the armed occurrence,
  `arm_timeout_after_commit` forwards the POST upstream — the service really
  commits the reservation — and only then answers `504` without relaying the
  upstream 200. The caller's retry races a commit it cannot see, exactly as
  on a real network: a fresh idempotency key duplicates (`booking-1` plus
  `booking-2`), a stable key returns the committed row. The transcript entry
  keeps the real `upstream_status` alongside `injected: timeout_after_commit`
  as the proof.
- **403-without-forward = real revocation.** While `set_revoked(True)`, the
  proxy answers mutating POSTs with `403` and never opens the upstream
  connection (`upstream_status: null`, `injected: permission_revoked`). No
  request means no commit is possible, so a mid-flow permission change
  cannot duplicate no matter how the agent retries; reads keep forwarding.

No model calls, no secrets, standard library only.
