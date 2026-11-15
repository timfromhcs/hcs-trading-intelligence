# Architecture

The CLI and loopback status API report current capabilities. The React shell reads the API from the same origin. The API records a startup event in local SQLite. The roadmap defines a modular monolith in which REST, MCP and the UI call shared application services. Domain and risk logic stay independent of providers and models. The future signer receives only independently validated plans.

The domain package defines identities, exact amounts and lifecycle transitions without importing transport or storage. Lifecycle transitions after an ambiguous submission require reconciliation before progressing to a known outcome. See the [repository brief](../AGENTS.md) for boundaries and acceptance criteria.
