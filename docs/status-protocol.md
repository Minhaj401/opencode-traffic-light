# Status coordination protocol

The status component aggregates session activity and serves it over loopback HTTP.
It does not launch widgets or configure desktop services.

## Endpoint compatibility

`GET http://127.0.0.1:4390/status` retains its existing response shape:

```json
{"state":"green"}
```

Valid colors are `green`, `yellow`, and `red`. Red has highest priority, then yellow,
then green. Existing widgets can continue using this endpoint without modification.

Legacy `POST /event` remains available:

```json
{"sid":"session-id","state":"yellow"}
```

## Heartbeats and ownership

Each process has a unique identity and retains its own session states. The first
process binds port 4390; peers send a full snapshot every second:

```text
POST /heartbeat
```

```json
{"processId":"unique-process-id","sessions":{"session-id":"yellow"}}
```

A snapshot replaces the previous one from that process, so deletion propagates
without guessing which process owns a session ID. Remote snapshots expire after
four seconds. Forwarded requests time out after 800 ms, and only one request is
in flight per process. Surviving idle processes retry ownership when the owner exits.

The server and interval are unreferenced so they do not keep a stopped application
alive. Diagnostic failures do not interrupt state updates.

## Task state

Pending/running task calls hold yellow even if their parent reports idle. Hook and
message-part identifiers are reconciled to avoid duplicate accounting. Terminal
parts, removals, and session cleanup release calls. Observed child-session activity
continues to contribute after background task return; waiting sessions take red
priority. Tool-name token matching avoids interpreting `task` as an `ask` prompt.

## Migration

Restart all application/backend processes after updating this component. Legacy
event-only clients have no process lifetime, so their state cannot expire safely.
They remain accepted but should be restarted to participate in heartbeat tracking.

The endpoint remains loopback-only and unauthenticated. Do not expose it externally.

## Tests

```sh
node --experimental-vm-modules --test tests/plugin.test.js
```

The suite isolates networking, time, and runtime APIs. It covers ownership takeover,
snapshot replacement/expiry, concurrent calls, task cleanup, input priority,
malformed payloads, and legacy event compatibility without running a desktop widget.
