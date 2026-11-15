# Architecture

The current package contains a CLI that reports capability status without connecting to providers or opening private state. The roadmap defines a modular monolith in which REST, MCP and the UI call shared application services. Domain and risk logic stay independent of providers and models. The future signer receives only independently validated plans.

The first application milestone adds canonical domain types, versioned SQLite state, audit events, a loopback API and a responsive shell. See the [repository brief](../AGENTS.md) for boundaries and acceptance criteria.
