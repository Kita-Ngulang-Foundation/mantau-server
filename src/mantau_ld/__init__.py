"""mantau_ld — Scenario 2's server half: accepts signed envelopes from agents
over an outbound tunnel connection. Never dials in to a customer's network;
that property is what makes this scenario CGNAT-proof (see
`../../protocol/PROTOCOL.md`).

    api/       FastAPI app, routes (ingest, agents, cameras, events, devices)
    ingest/    envelope signature verification + seq-based dedupe
    store/     SQLite persistence + the mantau_core protocol implementations
    alerts/    FallEvent -> mantau_core.notify.Fanout, same shape as
               mantau-backend-rtsp's own dispatcher -- deliberately similar
               code, not shared, so each backend can evolve independently
               until the eventual restructure decides what actually merges
"""

__version__ = "0.1.0"
