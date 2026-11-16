# ADR 0008 — REST path layout

Status: accepted.

## Context

AGENTS.md section 9.2 sketches `/api/v1/system/pause`, `/api/v1/system/resume` and `/api/v1/orders/live`. The implementation grew by feature.

## Decision

Pause and resume are under `/api/v1/risk/` because they change the risk state. Live canary submission and spot order reads share `/api/v1/orders/spot`. The brief names stay listed as planned in [the API reference](../API.md) and are not aliased, to avoid two entry points to one side effect. A new alias requires a test proving it calls the same use case.

## Consequences

- Clients use the implemented paths. The drift test checks that every path in `api.py` appears in the API reference.
