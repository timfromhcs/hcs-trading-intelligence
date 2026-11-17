"""Node-as-signer PSBT spend flow for BTC/LTC/DGB, enabled on regtest only.

Python never holds a private key here. The operator's own node wallet builds
(`walletcreatefundedpsbt`), signs (`walletprocesspsbt`) and finalizes the
transaction. This module's job is to independently decode every artefact the node
returns (`decodepsbt`, `decoderawtransaction`) and refuse to continue unless it
still matches the approved ``ValidatedTransactionPlan``.

Mainnet and public testnets raise ``UtxoSpendDisabled``; there is no override.
"""

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from hcs_trader.domain.signing import (
    SignedTransaction,
    SignerError,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)
from hcs_trader.signing import IsolatedSigner
from hcs_trader.wallets import UTXO_NETWORKS, LocalWalletRpc, UtxoNetwork

REGTEST_CHAIN_IDS = {
    "bitcoin:regtest": "btc-balance",
    "litecoin:regtest": "ltc-balance",
    "digibyte:regtest": "dgb-balance",
}
# Well-known Bitcoin regtest genesis. LTC/DGB values are not pinned here because
# they have not been verified against a running node; pass ``expected_genesis``.
BTC_REGTEST_GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"

_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_TXID = re.compile(r"[0-9a-f]{64}")
_MAX_PSBT_CHARS = 200_000
_SATS = Decimal(100_000_000)


class UtxoSpendDisabled(SignerPolicyViolation):
    """UTXO spending is only available on a regtest profile."""


class UtxoReconciliationRequired(SignerError):
    """A broadcast may or may not have happened; reconcile before retrying."""


@dataclass(frozen=True, slots=True)
class UtxoSpendResult:
    signed: SignedTransaction
    txid: str
    psbt_sha256: str
    fee_base_units: int
    change_base_units: int


@dataclass(slots=True)
class _LedgerEntry:
    plan_hash: str
    state: Literal[
        "BUILDING", "BROADCASTING", "RECONCILIATION_REQUIRED", "COMPLETED", "CONFLICTED"
    ]
    txid: str | None = None
    result: UtxoSpendResult | None = None
    # Captured before the broadcast attempt so reconcile() can build the result.
    raw_hex: str | None = None
    psbt_sha256: str | None = None
    fee: int = 0
    change: int = 0
    destination: str | None = None
    amount: int = 0
    plan_hash_signed: str | None = None


@dataclass(frozen=True, slots=True)
class _Output:
    address: str
    sats: int


@dataclass(frozen=True, slots=True)
class _Verified:
    outputs: tuple[_Output, ...]
    outpoints: tuple[tuple[str, int], ...]
    fee: int
    change: int


def _to_sats(value: object) -> int:
    if not isinstance(value, Decimal | int) or isinstance(value, bool):
        raise SignerTamperingError("Node returned a non-numeric coin amount")
    scaled = Decimal(value) * _SATS
    if scaled != scaled.to_integral_value() or scaled < 0:
        raise SignerTamperingError("Node returned an invalid coin amount")
    return int(scaled)


def _coins(sats: int) -> str:
    return f"{Decimal(sats) / _SATS:.8f}"


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SignerTamperingError(f"Node returned malformed {what}")
    return value


def _sequence(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise SignerTamperingError(f"Node returned malformed {what}")
    return value


def _output_address(vout: dict[str, Any]) -> str:
    script = _mapping(vout.get("scriptPubKey"), "scriptPubKey")
    address = script.get("address")
    if address is None:
        addresses = script.get("addresses")
        if isinstance(addresses, list) and len(addresses) == 1:
            address = addresses[0]
    if not isinstance(address, str) or not address:
        raise SignerTamperingError("Transaction has an output without an address")
    return address


def _parse_vouts(vouts: list[Any]) -> tuple[_Output, ...]:
    return tuple(
        _Output(
            _output_address(_mapping(v, "output")),
            _to_sats(_mapping(v, "output").get("value")),
        )
        for v in vouts
    )


def _parse_outpoints(vins: list[Any]) -> tuple[tuple[str, int], ...]:
    points: list[tuple[str, int]] = []
    for vin in vins:
        item = _mapping(vin, "input")
        txid, index = item.get("txid"), item.get("vout")
        if (
            not isinstance(txid, str)
            or not _TXID.fullmatch(txid)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            raise SignerTamperingError("Transaction input is not a plain outpoint")
        points.append((txid, index))
    if len(set(points)) != len(points):
        raise SignerTamperingError("Transaction spends the same outpoint twice")
    return tuple(points)


class UtxoPsbtExecutor:
    """Regtest-only build, verify, sign (in the node) and broadcast flow."""

    def __init__(
        self,
        rpc: LocalWalletRpc,
        network_key: str,
        signer: IsolatedSigner,
        wallet_name: str,
        *,
        fee_rate_coins_per_kvb: Decimal = Decimal("0.0001"),
        max_inputs: int = 20,
        mempool_check: bool = True,
        expected_genesis: str | None = None,
        pause_check: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if network_key not in UTXO_NETWORKS:
            raise ValueError("Unknown UTXO network")
        if fee_rate_coins_per_kvb <= 0:
            raise ValueError("fee rate must be positive")
        self._rpc = rpc
        self._network: UtxoNetwork = UTXO_NETWORKS[network_key]
        self._network_key = network_key
        self._signer = signer
        self._wallet_name = wallet_name
        self._fee_rate = fee_rate_coins_per_kvb
        self._max_inputs = max_inputs
        self._mempool_check = mempool_check
        self._expected_genesis = expected_genesis
        self._pause_check = pause_check
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._ledger: dict[str, _LedgerEntry] = {}

    def ledger_state(self, idempotency_key: str) -> str | None:
        entry = self._ledger.get(idempotency_key)
        return entry.state if entry else None

    # ------------------------------------------------------------------ helpers
    def _version(self) -> Literal["1.0", "2.0"]:
        return self._network.rpc_version

    async def _call(self, method: str, params: list[object]) -> object:
        return await self._rpc.call(method, params, version=self._version())

    def _require_regtest_plan(self, plan: ValidatedTransactionPlan) -> None:
        if REGTEST_CHAIN_IDS.get(plan.canonical_chain_id) != self._network_key:
            raise UtxoSpendDisabled(
                "UTXO spending is disabled except on the matching regtest profile "
                f"(plan chain '{plan.canonical_chain_id}')"
            )

    async def _require_regtest_node(self) -> None:
        info = _mapping(await self._call("getblockchaininfo", []), "chain info")
        if info.get("chain") != "regtest":
            raise UtxoSpendDisabled(
                "UTXO spending is disabled: node does not report the regtest chain"
            )
        if self._expected_genesis is not None:
            genesis = await self._call("getblockhash", [0])
            if genesis != self._expected_genesis:
                raise UtxoSpendDisabled("Node genesis does not match the profile")

    def _check_plan_fields(self, plan: ValidatedTransactionPlan) -> None:
        if plan.wallet_id != self._wallet_name:
            raise SignerPolicyViolation("Plan wallet does not match the node wallet")
        if plan.asset_in != plan.asset_out:
            raise SignerPolicyViolation("UTXO plans are same-asset transfers only")
        if plan.min_amount_out_base_units != plan.amount_in_base_units:
            raise SignerPolicyViolation("Transfer min output must equal the amount")

    async def _verify(
        self,
        decoded_tx: dict[str, Any],
        input_amounts: list[int] | None,
        plan: ValidatedTransactionPlan,
        node_fee: object = None,
    ) -> _Verified:
        outputs = _parse_vouts(_sequence(decoded_tx.get("vout"), "outputs"))
        outpoints = _parse_outpoints(_sequence(decoded_tx.get("vin"), "inputs"))
        if not outpoints or len(outpoints) > self._max_inputs:
            raise SignerTamperingError("Unexpected number of inputs")
        dest = plan.destination_id
        dest_outputs = [o for o in outputs if o.address == dest]
        if len(dest_outputs) != 1 or dest_outputs[0].sats != plan.amount_in_base_units:
            raise SignerTamperingError(
                "Outputs do not pay exactly the planned amount to the destination"
            )
        others = [o for o in outputs if o.address != dest]
        if len(others) > 1:
            raise SignerTamperingError("Unexpected extra outputs in transaction")
        change = 0
        if others:
            info = _mapping(
                await self._call("getaddressinfo", [others[0].address]),
                "address info",
            )
            if info.get("ismine") is not True:
                raise SignerTamperingError(
                    "Extra output does not pay a change address owned by the wallet"
                )
            change = others[0].sats
        fee = 0
        if input_amounts is not None:
            fee = sum(input_amounts) - sum(o.sats for o in outputs)
            if node_fee is not None and _to_sats(node_fee) != fee:
                raise SignerTamperingError("Node-reported fee differs from decoded fee")
            if fee < 0 or fee > plan.max_network_fee_base_units:
                raise SignerTamperingError(
                    f"Fee {fee} sats exceeds the planned cap "
                    f"{plan.max_network_fee_base_units}"
                )
        return _Verified(outputs, outpoints, fee, change)

    async def _decode_psbt(
        self, psbt: object, plan: ValidatedTransactionPlan
    ) -> _Verified:
        if (
            not isinstance(psbt, str)
            or len(psbt) > _MAX_PSBT_CHARS
            or not _BASE64.fullmatch(psbt)
        ):
            raise SignerTamperingError("Node returned a malformed PSBT")
        decoded = _mapping(await self._call("decodepsbt", [psbt]), "decoded PSBT")
        tx = _mapping(decoded.get("tx"), "PSBT transaction")
        vins = _sequence(tx.get("vin"), "inputs")
        inputs = _sequence(decoded.get("inputs"), "PSBT inputs")
        if len(inputs) != len(vins):
            raise SignerTamperingError("PSBT input metadata does not match inputs")
        amounts: list[int] = []
        for vin, meta in zip(vins, inputs, strict=True):
            item = _mapping(meta, "PSBT input")
            utxo = item.get("witness_utxo")
            if utxo is not None:
                amounts.append(_to_sats(_mapping(utxo, "witness utxo").get("amount")))
                continue
            prev = item.get("non_witness_utxo")
            index = _mapping(vin, "input").get("vout")
            if prev is None or not isinstance(index, int):
                raise SignerTamperingError("PSBT input has no UTXO information")
            vouts = _sequence(_mapping(prev, "previous tx").get("vout"), "previous")
            if not 0 <= index < len(vouts):
                raise SignerTamperingError("PSBT input references a missing output")
            amounts.append(_to_sats(_mapping(vouts[index], "previous").get("value")))
        return await self._verify(tx, amounts, plan, decoded.get("fee"))

    # ------------------------------------------------------------------ flow
    async def execute(
        self, plan: ValidatedTransactionPlan, at: datetime
    ) -> UtxoSpendResult:
        self._require_regtest_plan(plan)
        self._signer.policy.validate_plan(plan, at)
        self._check_plan_fields(plan)

        prior = self._ledger.get(plan.idempotency_key)
        if prior is not None:
            if prior.plan_hash != plan.plan_hash:
                raise SignerTamperingError("Idempotency key reused with another plan")
            if prior.state == "COMPLETED" and prior.result is not None:
                return prior.result
            raise UtxoReconciliationRequired(
                f"Earlier attempt is {prior.state}; reconcile before retrying"
            )
        entry = _LedgerEntry(plan.plan_hash, "BUILDING")
        self._ledger[plan.idempotency_key] = entry
        try:
            return await self._run(plan, at, entry)
        except (Exception, asyncio.CancelledError):
            if entry.state == "BUILDING":
                # Nothing was broadcast, so a retry is safe.
                self._ledger.pop(plan.idempotency_key, None)
            elif entry.state == "BROADCASTING":
                entry.state = "RECONCILIATION_REQUIRED"
            raise

    async def _run(
        self,
        plan: ValidatedTransactionPlan,
        at: datetime,
        entry: _LedgerEntry,
    ) -> UtxoSpendResult:
        await self._require_regtest_node()

        created = _mapping(
            await self._call(
                "walletcreatefundedpsbt",
                [
                    [],
                    [{plan.destination_id: _coins(plan.amount_in_base_units)}],
                    0,
                    {"feeRate": f"{self._fee_rate:.8f}"},
                    True,
                ],
            ),
            "funded PSBT",
        )
        built = await self._decode_psbt(created.get("psbt"), plan)
        built_psbt = str(created["psbt"])

        processed = _mapping(
            await self._call("walletprocesspsbt", [built_psbt, True, "ALL"]),
            "processed PSBT",
        )
        after = await self._decode_psbt(processed.get("psbt"), plan)
        if (after.outputs, after.outpoints) != (built.outputs, built.outpoints):
            raise SignerTamperingError("Signing changed the transaction structure")
        psbt_hash = hashlib.sha256(str(processed["psbt"]).encode()).hexdigest()

        final = _mapping(
            await self._call("finalizepsbt", [processed["psbt"], True]), "final PSBT"
        )
        raw_hex = final.get("hex")
        if (
            final.get("complete") is not True
            or not isinstance(raw_hex, str)
            or not _HEX.fullmatch(raw_hex)
        ):
            raise SignerTamperingError("Node could not finalize the PSBT")
        decoded_raw = _mapping(
            await self._call("decoderawtransaction", [raw_hex]), "raw transaction"
        )
        raw_check = await self._verify(decoded_raw, None, plan)
        if (raw_check.outputs, raw_check.outpoints) != (
            built.outputs,
            built.outpoints,
        ):
            raise SignerTamperingError("Finalized transaction differs from the plan")
        txid = decoded_raw.get("txid")
        if not isinstance(txid, str) or not _TXID.fullmatch(txid):
            raise SignerTamperingError("Node returned a malformed txid")

        if self._mempool_check:
            accept = _sequence(
                await self._call("testmempoolaccept", [[raw_hex]]), "mempool test"
            )
            if (
                len(accept) != 1
                or _mapping(accept[0], "mempool test").get("allowed") is not True
            ):
                raise SignerTamperingError("Node mempool test rejected the transaction")

        # Last gate before the side effect: pause/kill state and policy.
        if self._pause_check is not None and self._pause_check():
            raise SignerPolicyViolation("Broadcast rejected: system is paused")
        broadcast_at = self._clock()
        self._signer.policy.validate_plan(plan, broadcast_at)

        entry.state = "BROADCASTING"
        entry.txid = txid
        entry.raw_hex = raw_hex
        entry.psbt_sha256 = psbt_hash
        entry.fee = built.fee
        entry.change = built.change
        entry.destination = plan.destination_id
        entry.amount = plan.amount_in_base_units
        entry.plan_hash_signed = plan.plan_hash
        sent = await self._call("sendrawtransaction", [raw_hex])
        if sent != txid:
            raise SignerTamperingError("Broadcast txid differs from decoded txid")

        result = UtxoSpendResult(
            SignedTransaction(plan.plan_hash, raw_hex, txid, broadcast_at),
            txid,
            psbt_hash,
            built.fee,
            built.change,
        )
        entry.state = "COMPLETED"
        entry.result = result
        return result

    async def reconcile(self, idempotency_key: str) -> str:
        """Resolve an ambiguous broadcast by asking the node wallet about the txid.

        Returns ``COMPLETED`` (result stored; later ``execute`` replays it),
        ``RECONCILIATION_REQUIRED`` while the node does not know the transaction
        yet, or ``CONFLICTED`` (terminal) when the node reports it conflicted,
        abandoned or double-spent. ``CONFLICTED`` never auto-retries: the operator
        must review it, and ``execute`` with that key keeps raising.
        """
        entry = self._ledger.get(idempotency_key)
        if entry is None:
            return "UNKNOWN"
        if entry.state in ("COMPLETED", "CONFLICTED"):
            return entry.state
        if entry.txid is None or entry.raw_hex is None:
            raise UtxoReconciliationRequired("No txid recorded; operator review needed")
        info = _mapping(await self._call("gettransaction", [entry.txid]), "tx info")
        if info.get("txid") != entry.txid:
            raise UtxoReconciliationRequired("Node returned another transaction")
        confirmations = info.get("confirmations")
        if (
            not isinstance(confirmations, int)
            or isinstance(confirmations, bool)
            or confirmations < 0
            or info.get("abandoned") is True
            or _sequence(info.get("walletconflicts", []), "conflicts")
        ):
            entry.state = "CONFLICTED"
            return entry.state
        self._check_wallet_payment(info, entry)
        entry.result = UtxoSpendResult(
            SignedTransaction(
                entry.plan_hash_signed or entry.plan_hash,
                entry.raw_hex,
                entry.txid,
                self._clock(),
            ),
            entry.txid,
            entry.psbt_sha256 or "",
            entry.fee,
            entry.change,
        )
        entry.state = "COMPLETED"
        return entry.state

    @staticmethod
    def _check_wallet_payment(info: dict[str, Any], entry: _LedgerEntry) -> None:
        details = info.get("details")
        if details is None:
            return
        paid = [
            d
            for d in _sequence(details, "details")
            if isinstance(d, dict) and d.get("address") == entry.destination
        ]
        if len(paid) != 1 or paid[0].get("category") != "send":
            raise UtxoReconciliationRequired("Node wallet shows no matching payment")
        amount = paid[0].get("amount")
        if not isinstance(amount, Decimal | int) or isinstance(amount, bool):
            raise UtxoReconciliationRequired("Node wallet payment amount is malformed")
        if -_to_sats(-Decimal(amount)) != -entry.amount:
            raise UtxoReconciliationRequired("Node wallet payment amount differs")
