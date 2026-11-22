# Execution-path invariants (AGENTS.md section 13.2)

Each invariant is mapped to the named tests that exercise it. `tests/test_invariant_map.py`
parses the table below and fails when an invariant has no test, when a referenced test does
not exist, or when AGENTS.md section 13.2 no longer has exactly twelve invariants. The map
shows where coverage lives; it does not by itself prove the invariant. Gaps are listed under
"Known gaps" and in `docs/KNOWN_LIMITATIONS.md`.

| # | Invariant | Tests |
|---|---|---|
| 1 | If data is stale, no live order is submitted. | `tests/test_risk.py::test_risk_rejects_stale_or_missing_quote`, `tests/test_risk.py::test_health_reconcile_and_market_gates`, `tests/test_mcp.py::test_preview_tool_reports_stale_quote_and_writes_nothing` |
| 2 | If live mode is false, no live order is submitted. | `tests/test_risk.py::test_risk_rejects_when_live_disabled`, `tests/test_signing.py::test_invariant_live_mode_false_rejects`, `tests/test_live_readiness.py::test_fresh_install_is_paper` |
| 3 | If the kill switch is set, no live order is submitted. | `tests/test_risk.py::test_risk_rejects_when_kill_switch_active`, `tests/test_signing.py::test_invariant_kill_switch_active_rejects`, `tests/test_exec_wiring.py::test_persisted_pause_blocks_before_any_venue_call` |
| 4 | If risk rejects, the signer is never called. | `tests/test_signing.py::test_rejected_risk_decision_blocks_signing`, `tests/test_signing.py::test_unknown_risk_decision_blocks_signing`, `tests/test_signing.py::test_rejected_stored_decision_blocks_signing`, `tests/test_evm_signing.py::test_unapproved_risk_decision_blocks_evm_signing` |
| 5 | If a plan hash differs from the approved hash, the signer rejects. | `tests/test_signing.py::test_invariant_plan_hash_mismatch_rejects`, `tests/test_signing.py::test_risk_decision_for_different_plan_hash_blocks_signing`, `tests/test_solana_signing.py::test_plan_hash_tamper_and_non_solana_chain_rejected`, `tests/test_risk.py::test_plan_hash_bound_and_mutation_changes_it` |
| 6 | If a destination, token or program is not allowlisted, the signer rejects. | `tests/test_signing.py::test_invariant_destination_not_allowlisted_rejects`, `tests/test_evm_signing.py::test_non_allowlisted_router_and_token_rejected`, `tests/test_solana_signing.py::test_unknown_program_rejected` |
| 7 | If a quote is expired or slippage exceeds policy, the signer rejects. | `tests/test_signing.py::test_invariant_expired_deadline_rejects`, `tests/test_signing.py::test_invariant_slippage_excess_rejects`, `tests/test_solana_signing.py::test_replay_after_deadline_rejected` |
| 8 | If a transaction times out after broadcast, retry logic reconciles before any second submission. | `tests/test_kraken_spot.py::test_submit_spot_order_timeout_requires_reconciliation`, `tests/test_kraken_spot.py::test_kraken_reconciliation_after_reopen_never_resubmits`, `tests/test_exec_wiring.py::test_ambiguous_submit_trips_reconciliation_required` |
| 9 | An MCP client cannot access signer secrets or expand its own scope. | `tests/test_mcp.py::test_mcp_and_application_never_import_signer_or_keystore`, `tests/test_signing.py::test_mcp_server_has_no_secret_export_or_signing_tools`, `tests/security/test_permission_matrix.py::test_real_server_denies_every_tool_without_grant`, `tests/test_mcp.py::test_higher_tier_tool_is_rejected_under_default_read_grant` |
| 10 | External news instructions cannot cause a transfer, swap or configuration change. | `tests/security/test_prompt_injection.py::test_hostile_news_via_mcp_causes_no_side_effect`, `tests/security/test_prompt_injection.py::test_hostile_model_output_fails_schema_so_it_cannot_carry_actions` |
| 11 | Restart does not erase order state, risk state, the kill switch or audit history. | `tests/test_crash_recovery.py::test_kill_switch_survives_hard_process_exit`, `tests/chaos/test_storage_faults.py::test_order_lifecycle_survives_a_restart_between_every_transition`, `tests/chaos/test_storage_faults.py::test_kill_switch_trip_survives_restart_and_blocks_reservation`, `tests/test_signing.py::test_persisted_kill_switch_blocks_signing_across_restart` |
| 12 | Cold reserve keys cannot be derived from or accessed through the application. | `tests/test_keystore.py::test_cold_and_watch_only_wallets_never_get_keystore_records`, `tests/test_keystore.py::test_records_marked_non_hot_cannot_be_decrypted_or_stored`, `tests/test_keystore.py::test_only_signing_and_keystore_modules_touch_decryption` |

## Known gaps

- Invariant 8 is covered for the Kraken spot adapter and the execution wiring. Broadcast
  timeouts for Jupiter, EVM and UTXO routes are not exercised on a live path because those
  routes cannot broadcast from this repository.
- Invariant 12 shows that the application never stores a cold or watch-only key. It cannot
  show that a user kept a cold key out of the hot wallet; that is a custody decision.
- Invariants 1 and 3 are unit-tested on the risk engine and signer; end-to-end proof with a
  real venue remains in the rehearsal evidence listed in `docs/KNOWN_LIMITATIONS.md`.
