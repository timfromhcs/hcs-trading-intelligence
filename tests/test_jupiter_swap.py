"""Tests for Solana Jupiter swap route verification and safety bounds."""

import base64
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hcs_trader.jupiter_swap import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    JUPITER_PROGRAM_ALLOWLIST_VERSION,
    JUPITER_V6_PROGRAM_ID,
    SPL_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    JupiterRouteError,
    JupiterRouteQuote,
    JupiterSwapAdapter,
    normalize_build_instructions,
)

_SOL_MINT = "So11111111111111111111111111111111111111112"
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SUSPICIOUS_MINT = "BadMint111111111111111111111111111111111111"
_SUSPICIOUS_PROGRAM = "MaliciousProgram111111111111111111111111111"


def _sample_quote(
    in_mint: str = _SOL_MINT,
    out_mint: str = _USDC_MINT,
    in_amount: int = 1000000000,  # 1 SOL
    out_amount: int = 150000000,  # 150 USDC
    slippage_bps: int = 50,
    price_impact_pct: str = "0.05",
    age_seconds: int = 5,
    route_plan: list[dict[str, object]] | None = None,
) -> JupiterRouteQuote:
    now = datetime.now(UTC)
    return JupiterRouteQuote(
        input_mint=in_mint,
        output_mint=out_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        other_amount_threshold=int(out_amount * 0.995),
        slippage_bps=slippage_bps,
        price_impact_pct=Decimal(price_impact_pct),
        route_plan=route_plan
        or [
            {
                "swapInfo": {
                    "ammKey": "amm-key-01",
                    "label": "Whirlpool",
                    "inputMint": in_mint,
                    "outputMint": out_mint,
                }
            }
        ],
        quoted_at=now - timedelta(seconds=age_seconds),
    )


def test_validate_route_success() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote()
    now = datetime.now(UTC)

    # Should pass without raising
    adapter.validate_route(
        quote,
        max_slippage_bps=100,
        max_price_impact_pct=Decimal("0.50"),
        at=now,
    )


def test_validate_route_unapproved_input_mint() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(in_mint=_SUSPICIOUS_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="Input mint.*not allowlisted"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_unapproved_output_mint() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(out_mint=_SUSPICIOUS_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="Output mint.*not allowlisted"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_identical_mints() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(in_mint=_SOL_MINT, out_mint=_SOL_MINT)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="identical"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_stale_quote() -> None:
    adapter = JupiterSwapAdapter(max_quote_age_seconds=30)
    quote = _sample_quote(age_seconds=60)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="freshness window"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_excess_slippage() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(slippage_bps=150)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="slippage.*exceeds policy cap"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_excess_price_impact() -> None:
    adapter = JupiterSwapAdapter()
    quote = _sample_quote(price_impact_pct="1.50")
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="price impact.*exceeds policy cap"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_validate_route_unapproved_intermediate_token() -> None:
    adapter = JupiterSwapAdapter()
    bad_hop_plan: list[dict[str, object]] = [
        {
            "swapInfo": {
                "label": "Raydium",
                "inputMint": _SOL_MINT,
                "outputMint": _SUSPICIOUS_MINT,
            }
        }
    ]
    quote = _sample_quote(route_plan=bad_hop_plan)
    now = datetime.now(UTC)

    with pytest.raises(JupiterRouteError, match="unapproved output token"):
        adapter.validate_route(
            quote,
            max_slippage_bps=100,
            max_price_impact_pct=Decimal("0.50"),
            at=now,
        )


def test_inspect_swap_instructions_success() -> None:
    adapter = JupiterSwapAdapter()
    payload = {
        "instructions": [
            {"program_id": "ComputeBudget111111111111111111111111111111"},
            {"program_id": JUPITER_V6_PROGRAM_ID},
            {"program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        ]
    }
    # Should pass without raising
    adapter.inspect_swap_instructions(payload)


def test_inspect_swap_instructions_unapproved_program() -> None:
    adapter = JupiterSwapAdapter()
    payload = {
        "instructions": [
            {"program_id": JUPITER_V6_PROGRAM_ID},
            {"program_id": _SUSPICIOUS_PROGRAM},
        ]
    }
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_swap_instructions(payload)


def test_inspect_swap_instructions_empty() -> None:
    adapter = JupiterSwapAdapter()
    with pytest.raises(JupiterRouteError, match="contains no instructions"):
        adapter.inspect_swap_instructions({"instructions": []})


# ---------------------------------------------------------------------------
# Jupiter Swap API V2 /build response handling.
#
# Fixtures are derived from the documented BuildResponse type at
# https://developers.jup.ag/docs/swap/build (base URL https://api.jup.ag/swap/v2).
# They are synthetic: no live Jupiter API is called in tests.
# ---------------------------------------------------------------------------

_SIGNER = "Signer1111111111111111111111111111111111111"
_OTHER_OWNER = "Attacker111111111111111111111111111111111111"
_ATA = "AtaAccount11111111111111111111111111111111111"
_COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _account(
    pubkey: str, *, signer: bool = False, writable: bool = False
) -> dict[str, Any]:
    return {"pubkey": pubkey, "isSigner": signer, "isWritable": writable}


def _ata_create_idempotent(
    *,
    owner: str = _SIGNER,
    payer: str = _SIGNER,
    mint: str = _USDC_MINT,
    token_program: str = SPL_TOKEN_PROGRAM_ID,
    data: bytes = b"\x01",
) -> dict[str, Any]:
    return {
        "programId": ASSOCIATED_TOKEN_PROGRAM_ID,
        "accounts": [
            _account(payer, signer=True, writable=True),
            _account(_ATA, writable=True),
            _account(owner),
            _account(mint),
            _account(SYSTEM_PROGRAM_ID),
            _account(token_program),
        ],
        "data": _b64(data),
    }


def _build_response() -> dict[str, Any]:
    """Doc-derived V2 /build response (BuildResponse type)."""
    return {
        "inputMint": _SOL_MINT,
        "outputMint": _USDC_MINT,
        "inAmount": "1000000000",
        "outAmount": "150000000",
        "otherAmountThreshold": "149250000",
        "swapMode": "ExactIn",
        "slippageBps": 50,
        "routePlan": [
            {
                "percent": 100,
                "bps": 10000,
                "swapInfo": {
                    "ammKey": "amm-key-01",
                    "label": "Whirlpool",
                    "inputMint": _SOL_MINT,
                    "outputMint": _USDC_MINT,
                    "inAmount": "1000000000",
                    "outAmount": "150000000",
                },
            }
        ],
        "computeBudgetInstructions": [
            {
                "programId": _COMPUTE_BUDGET,
                "accounts": [],
                "data": _b64(b"\x02\x40\x0d\x03\x00"),
            }
        ],
        "setupInstructions": [_ata_create_idempotent()],
        "swapInstruction": {
            "programId": JUPITER_V6_PROGRAM_ID,
            "accounts": [_account(_SIGNER, signer=True, writable=True)],
            "data": _b64(b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a"),
        },
        "cleanupInstruction": None,
        "otherInstructions": [],
        "tipInstruction": None,
        "addressesByLookupTableAddress": None,
        "blockhashWithMetadata": {"blockhash": [1] * 32, "lastValidBlockHeight": 100},
    }


def test_normalize_build_orders_all_instruction_fields() -> None:
    response = _build_response()
    response["cleanupInstruction"] = {
        "programId": SPL_TOKEN_PROGRAM_ID,
        "accounts": [_account(_SIGNER, signer=True, writable=True)],
        "data": _b64(b"\x09"),
    }
    response["otherInstructions"] = [
        {"programId": SYSTEM_PROGRAM_ID, "accounts": [], "data": _b64(b"\x02")}
    ]
    response["tipInstruction"] = {
        "programId": SYSTEM_PROGRAM_ID,
        "accounts": [],
        "data": _b64(b"\x02\x00"),
    }
    ordered = normalize_build_instructions(response)
    assert [i["programId"] for i in ordered] == [
        _COMPUTE_BUDGET,
        ASSOCIATED_TOKEN_PROGRAM_ID,
        JUPITER_V6_PROGRAM_ID,
        SPL_TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
    ]
    assert ordered[-1]["data"] == _b64(b"\x02\x00")


def test_normalize_build_skips_null_cleanup_and_tip() -> None:
    ordered = normalize_build_instructions(_build_response())
    assert len(ordered) == 3


def test_normalize_build_rejects_unknown_instruction_key() -> None:
    response = _build_response()
    response["extraInstructions"] = []
    with pytest.raises(JupiterRouteError, match="Unknown instruction field"):
        normalize_build_instructions(response)


def test_normalize_build_requires_swap_instruction() -> None:
    response = _build_response()
    del response["swapInstruction"]
    with pytest.raises(JupiterRouteError, match="swapInstruction"):
        normalize_build_instructions(response)


def test_normalize_build_rejects_malformed_instruction() -> None:
    response = _build_response()
    response["setupInstructions"] = [{"programId": SYSTEM_PROGRAM_ID, "accounts": []}]
    with pytest.raises(JupiterRouteError, match="data"):
        normalize_build_instructions(response)


def test_normalize_build_rejects_list_field_with_wrong_type() -> None:
    response = _build_response()
    response["otherInstructions"] = {"programId": SYSTEM_PROGRAM_ID}
    with pytest.raises(JupiterRouteError, match="otherInstructions"):
        normalize_build_instructions(response)


def test_inspect_v2_build_response_success() -> None:
    adapter = JupiterSwapAdapter()
    adapter.inspect_swap_instructions(_build_response(), signer=_SIGNER)


def test_inspect_v2_build_unknown_program_in_other_instructions() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["otherInstructions"] = [
        {"programId": _SUSPICIOUS_PROGRAM, "accounts": [], "data": _b64(b"\x00")}
    ]
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_build_unknown_program_in_tip_instruction() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["tipInstruction"] = {
        "programId": _SUSPICIOUS_PROGRAM,
        "accounts": [],
        "data": _b64(b"\x00"),
    }
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_swap_instruction_must_be_jupiter() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["swapInstruction"]["programId"] = SYSTEM_PROGRAM_ID
    with pytest.raises(JupiterRouteError, match="swapInstruction"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_token_2022_program_rejected() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [
        {"programId": TOKEN_2022_PROGRAM_ID, "accounts": [], "data": _b64(b"\x01")}
    ]
    with pytest.raises(JupiterRouteError, match="unapproved program"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_allowlist_is_versioned_and_includes_ata_but_not_token_2022() -> None:
    adapter = JupiterSwapAdapter()
    assert JUPITER_PROGRAM_ALLOWLIST_VERSION
    assert ASSOCIATED_TOKEN_PROGRAM_ID == "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
    assert ASSOCIATED_TOKEN_PROGRAM_ID in adapter.allowed_programs
    assert TOKEN_2022_PROGRAM_ID not in adapter.allowed_programs


def test_inspect_v2_ata_requires_signer_argument() -> None:
    adapter = JupiterSwapAdapter()
    with pytest.raises(JupiterRouteError, match="signer"):
        adapter.inspect_swap_instructions(_build_response())


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00", b"\x02", b"\x01\x00"],
    ids=["create-empty", "create", "recover-nested", "trailing-bytes"],
)
def test_inspect_v2_ata_only_create_idempotent(data: bytes) -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [_ata_create_idempotent(data=data)]
    with pytest.raises(JupiterRouteError, match="CreateIdempotent"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_owner_must_be_signer() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [_ata_create_idempotent(owner=_OTHER_OWNER)]
    with pytest.raises(JupiterRouteError, match="owner"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_payer_must_be_signer() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [_ata_create_idempotent(payer=_OTHER_OWNER)]
    with pytest.raises(JupiterRouteError, match="payer"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_rejects_token_2022_token_program() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [
        _ata_create_idempotent(token_program=TOKEN_2022_PROGRAM_ID)
    ]
    with pytest.raises(JupiterRouteError, match="token program"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_rejects_unallowlisted_mint() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    response["setupInstructions"] = [_ata_create_idempotent(mint=_SUSPICIOUS_MINT)]
    with pytest.raises(JupiterRouteError, match="mint"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_rejects_wrong_account_count() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    ata = _ata_create_idempotent()
    ata["accounts"] = ata["accounts"][:5]
    response["setupInstructions"] = [ata]
    with pytest.raises(JupiterRouteError, match="accounts"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_ata_rejects_invalid_base64() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    ata = _ata_create_idempotent()
    ata["data"] = "not base64!!"
    response["setupInstructions"] = [ata]
    with pytest.raises(JupiterRouteError, match="base64"):
        adapter.inspect_swap_instructions(response, signer=_SIGNER)


def test_inspect_v2_does_not_mutate_response() -> None:
    adapter = JupiterSwapAdapter()
    response = _build_response()
    snapshot = copy.deepcopy(response)
    adapter.inspect_swap_instructions(response, signer=_SIGNER)
    assert response == snapshot


def test_legacy_ata_instruction_without_data_is_rejected() -> None:
    adapter = JupiterSwapAdapter()
    payload = {"instructions": [{"program_id": ASSOCIATED_TOKEN_PROGRAM_ID}]}
    with pytest.raises(JupiterRouteError):
        adapter.inspect_swap_instructions(payload, signer=_SIGNER)
