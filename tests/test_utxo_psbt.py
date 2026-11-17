"""Regtest-only node-as-signer PSBT flow: fake JSON-RPC node and optional real node."""

import dataclasses
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest

from hcs_trader.domain.signing import (
    SignerPolicy,
    SignerPolicyViolation,
    SignerTamperingError,
    ValidatedTransactionPlan,
)
from hcs_trader.signing import IsolatedSigner
from hcs_trader.utxo_psbt import (
    UtxoPsbtExecutor,
    UtxoReconciliationRequired,
    UtxoSpendDisabled,
)
from hcs_trader.wallets import LocalWalletRpc

DEST = "bcrt1qdestination0000000000000000000000000"
CHANGE = "bcrt1qchange000000000000000000000000000000"
STRANGER = "bcrt1qstranger00000000000000000000000000"
AMOUNT = 10_000_000
INPUT = 100_000_000
MAX_FEE = 20_000
NOW = datetime(2026, 1, 1, tzinfo=UTC)
_TXID = "ab" * 32
_PREV = "cd" * 32
_RAW_TXID = "ef" * 32


def _coins(sats: int) -> float:
    return float(Decimal(sats) / Decimal(100_000_000))


def _decoded(outputs: list[tuple[str, int]], input_sats: int = INPUT) -> dict[str, Any]:
    paid = sum(s for _, s in outputs)
    return {
        "tx": {
            "txid": _TXID,
            "vin": [{"txid": _PREV, "vout": 0}],
            "vout": [
                {"value": _coins(s), "n": i, "scriptPubKey": {"address": a}}
                for i, (a, s) in enumerate(outputs)
            ],
        },
        "inputs": [{"witness_utxo": {"amount": _coins(input_sats)}}],
        "fee": _coins(input_sats - paid),
    }


class FakeNode:
    def __init__(self, version: str = "2.0") -> None:
        self.version = version
        self.chain = "regtest"
        self.calls: list[str] = []
        good = [(DEST, AMOUNT), (CHANGE, INPUT - AMOUNT - 1_000)]
        self.built = _decoded(good)
        self.processed = _decoded(good)
        self.raw: dict[str, Any] = {
            "txid": _RAW_TXID,
            "vin": [{"txid": _PREV, "vout": 0}],
            "vout": self.built["tx"]["vout"],
        }
        self.finalize_complete = True
        self.mempool_allowed = True
        self.send_fails = False
        self.sent_txid = _RAW_TXID
        self.mine = {CHANGE}
        self.genesis = "00" * 32
        self.wallet_tx: dict[str, Any] = {
            "txid": _RAW_TXID,
            "confirmations": 0,
            "details": [
                {"address": DEST, "category": "send", "amount": -_coins(AMOUNT)}
            ],
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        result: object
        if method == "getblockchaininfo":
            result = {"chain": self.chain, "initialblockdownload": False}
        elif method == "walletcreatefundedpsbt":
            assert params[1] == [{DEST: "0.10000000"}]
            result = {"psbt": "QQ==", "fee": 0.00001, "changepos": 1}
        elif method == "decodepsbt":
            result = self.built if params[0] == "QQ==" else self.processed
        elif method == "getblockhash":
            result = self.genesis
        elif method == "getaddressinfo":
            result = {"ismine": params[0] in self.mine}
        elif method == "walletprocesspsbt":
            result = {"psbt": "Qg==", "complete": True}
        elif method == "finalizepsbt":
            result = {"hex": "0200aa", "complete": self.finalize_complete}
        elif method == "decoderawtransaction":
            result = self.raw
        elif method == "testmempoolaccept":
            result = [{"allowed": self.mempool_allowed}]
        elif method == "sendrawtransaction":
            if self.send_fails:
                return httpx2.Response(500, json={})
            result = self.sent_txid
        elif method == "gettransaction":
            result = self.wallet_tx
        else:
            raise AssertionError(method)
        envelope: dict[str, Any] = {"id": body["id"], "result": result, "error": None}
        if self.version == "2.0":
            envelope["jsonrpc"] = "2.0"
        return httpx2.Response(200, json=envelope)


def _plan(
    chain: str = "bitcoin:regtest",
    *,
    key: str = "idem-1",
    dest: str = DEST,
    max_fee: int = MAX_FEE,
) -> ValidatedTransactionPlan:
    return ValidatedTransactionPlan(
        canonical_chain_id=chain,
        network="regtest",
        wallet_id="w1",
        signer_address="node-wallet",
        asset_in="bitcoin:regtest/native:BTC",
        asset_out="bitcoin:regtest/native:BTC",
        amount_in_base_units=AMOUNT,
        min_amount_out_base_units=AMOUNT,
        destination_id=dest,
        max_slippage_bps=0,
        max_network_fee_base_units=max_fee,
        max_priority_fee_base_units=0,
        idempotency_key=key,
        risk_decision_id="risk-1",
        risk_policy_version="p1",
        simulation_success=True,
        simulation_block_reference="regtest-tip",
    ).with_hash()


def _signer(*, live: bool = True, kill: bool = False) -> IsolatedSigner:
    chains = frozenset({"bitcoin:regtest", "bitcoin:mainnet", "litecoin:regtest"})
    return IsolatedSigner(
        SignerPolicy(
            allowed_chain_ids=chains,
            allowed_destinations=frozenset({DEST, STRANGER}),
            allowed_tokens=frozenset(),
            max_slippage_bps_limit=0,
            max_network_fee_limit=MAX_FEE,
            live_mode_enabled=live,
            kill_switch_active=kill,
        ),
        pause_state=lambda: False,
        risk_decision_lookup=lambda _decision_id: None,
    )


def _executor(
    client: httpx2.AsyncClient,
    network_key: str = "btc-balance",
    signer: IsolatedSigner | None = None,
    pause: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
    **extra: Any,
) -> UtxoPsbtExecutor:
    return UtxoPsbtExecutor(
        LocalWalletRpc(client, 18443, "w1"),
        network_key,
        signer or _signer(),
        "w1",
        pause_check=pause,
        clock=clock,
        **extra,
    )


async def _run(
    node: FakeNode,
    plan: ValidatedTransactionPlan | None = None,
    *,
    network_key: str = "btc-balance",
    signer: IsolatedSigner | None = None,
    pause: Callable[[], bool] | None = None,
) -> Any:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c, network_key, signer, pause)
        return await ex.execute(plan or _plan(), NOW)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("version", ["2.0", "1.0"])
async def test_happy_path_broadcasts_once_with_independent_checks(
    version: str,
) -> None:
    node = FakeNode(version)
    key = "btc-balance" if version == "2.0" else "ltc-balance"
    plan = _plan("bitcoin:regtest" if version == "2.0" else "litecoin:regtest")
    result = await _run(node, plan, network_key=key)
    assert result.txid == _RAW_TXID
    assert result.fee_base_units == 1_000
    assert result.change_base_units == INPUT - AMOUNT - 1_000
    assert result.signed.plan_hash == plan.plan_hash
    assert node.calls.count("sendrawtransaction") == 1
    assert node.calls.index("decodepsbt") < node.calls.index("walletprocesspsbt")
    assert node.calls.index("decoderawtransaction") < node.calls.index(
        "sendrawtransaction"
    )


@pytest.mark.anyio
async def test_extra_output_to_stranger_is_rejected() -> None:
    node = FakeNode()
    node.built = _decoded([(DEST, AMOUNT), (STRANGER, INPUT - AMOUNT - 1_000)])
    with pytest.raises(SignerTamperingError, match="change address"):
        await _run(node)
    assert "walletprocesspsbt" not in node.calls


@pytest.mark.anyio
async def test_three_outputs_rejected() -> None:
    node = FakeNode()
    node.built = _decoded(
        [(DEST, AMOUNT), (CHANGE, 1_000), (CHANGE, INPUT - AMOUNT - 3_000)]
    )
    with pytest.raises(SignerTamperingError, match="extra outputs"):
        await _run(node)


@pytest.mark.anyio
async def test_changed_amount_is_rejected() -> None:
    node = FakeNode()
    node.built = _decoded([(DEST, AMOUNT + 1), (CHANGE, INPUT - AMOUNT - 1_001)])
    with pytest.raises(SignerTamperingError, match="planned amount"):
        await _run(node)


@pytest.mark.anyio
async def test_swapped_destination_is_rejected() -> None:
    node = FakeNode()
    node.built = _decoded([(STRANGER, AMOUNT), (CHANGE, INPUT - AMOUNT - 1_000)])
    with pytest.raises(SignerTamperingError, match="planned amount"):
        await _run(node)


@pytest.mark.anyio
async def test_excess_fee_is_rejected() -> None:
    node = FakeNode()
    node.built = _decoded([(DEST, AMOUNT), (CHANGE, INPUT - AMOUNT - 50_000)])
    with pytest.raises(SignerTamperingError, match="exceeds the planned cap"):
        await _run(node)
    assert "sendrawtransaction" not in node.calls


@pytest.mark.anyio
async def test_negative_fee_and_fee_field_mismatch_rejected() -> None:
    node = FakeNode()
    node.built = _decoded([(DEST, AMOUNT), (CHANGE, INPUT)])
    with pytest.raises(SignerTamperingError):
        await _run(node)
    node2 = FakeNode()
    node2.built["fee"] = 0.5
    with pytest.raises(SignerTamperingError, match="Node-reported fee"):
        await _run(node2)


@pytest.mark.anyio
async def test_signing_step_that_changes_outputs_is_rejected() -> None:
    node = FakeNode()
    node.processed = _decoded([(DEST, AMOUNT), (CHANGE, INPUT - AMOUNT - 5_000)])
    with pytest.raises(SignerTamperingError, match="structure"):
        await _run(node)
    assert "finalizepsbt" not in node.calls


@pytest.mark.anyio
async def test_finalized_transaction_with_extra_output_is_rejected() -> None:
    node = FakeNode()
    node.raw["vout"] = [
        *node.raw["vout"],
        {"value": 0.0001, "n": 2, "scriptPubKey": {"address": STRANGER}},
    ]
    with pytest.raises(SignerTamperingError, match="extra outputs"):
        await _run(node)
    assert "sendrawtransaction" not in node.calls


@pytest.mark.anyio
async def test_unfinalizable_and_mempool_rejection() -> None:
    node = FakeNode()
    node.finalize_complete = False
    with pytest.raises(SignerTamperingError, match="finalize"):
        await _run(node)
    node2 = FakeNode()
    node2.mempool_allowed = False
    with pytest.raises(SignerTamperingError, match="mempool"):
        await _run(node2)
    assert "sendrawtransaction" not in node2.calls


@pytest.mark.anyio
@pytest.mark.parametrize("chain", ["main", "test", "testnet4", "signet"])
async def test_non_regtest_node_chain_is_disabled(chain: str) -> None:
    node = FakeNode()
    node.chain = chain
    with pytest.raises(UtxoSpendDisabled):
        await _run(node)
    assert node.calls == ["getblockchaininfo"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "chain", ["bitcoin:mainnet", "bitcoin:testnet", "litecoin:regtest"]
)
async def test_non_regtest_or_mismatched_plan_chain_is_disabled(chain: str) -> None:
    node = FakeNode()
    with pytest.raises(UtxoSpendDisabled):
        await _run(node, _plan(chain))
    assert node.calls == []


@pytest.mark.anyio
async def test_kill_switch_live_off_and_pause_block_everything() -> None:
    node = FakeNode()
    with pytest.raises(SignerPolicyViolation, match="kill switch"):
        await _run(node, signer=_signer(kill=True))
    with pytest.raises(SignerPolicyViolation, match="live mode"):
        await _run(node, signer=_signer(live=False))
    assert node.calls == []
    with pytest.raises(SignerPolicyViolation, match="paused"):
        await _run(node, pause=lambda: True)
    assert "sendrawtransaction" not in node.calls


@pytest.mark.anyio
async def test_unlisted_destination_and_tampered_plan_rejected() -> None:
    node = FakeNode()
    with pytest.raises(SignerPolicyViolation, match="not allowlisted"):
        await _run(node, _plan(dest="bcrt1qother"))
    forged = dataclasses.replace(_plan(), amount_in_base_units=AMOUNT + 1)
    with pytest.raises(SignerTamperingError, match="hash mismatch"):
        await _run(node, forged)
    assert node.calls == []


@pytest.mark.anyio
async def test_idempotent_replay_and_reconciliation_after_ambiguous_send() -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c)
        first = await ex.execute(_plan(), NOW)
        again = await ex.execute(_plan(), NOW)
        assert again is first and node.calls.count("sendrawtransaction") == 1
        with pytest.raises(SignerTamperingError, match="another plan"):
            await ex.execute(_plan(dest=STRANGER), NOW)

        node.send_fails = True
        with pytest.raises(Exception, match="request failed"):
            await ex.execute(_plan(key="idem-2"), NOW)
        assert ex.ledger_state("idem-2") == "RECONCILIATION_REQUIRED"
        with pytest.raises(UtxoReconciliationRequired):
            await ex.execute(_plan(key="idem-2"), NOW)
        assert node.calls.count("sendrawtransaction") == 2
        assert await ex.reconcile("idem-2") == "COMPLETED"
        stored = await ex.execute(_plan(key="idem-2"), NOW)
        assert stored.txid == _RAW_TXID and stored.fee_base_units == 1_000
        assert node.calls.count("sendrawtransaction") == 2


@pytest.mark.anyio
async def test_failure_before_broadcast_allows_safe_retry() -> None:
    node = FakeNode()
    node.finalize_complete = False
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c)
        with pytest.raises(SignerTamperingError):
            await ex.execute(_plan(), NOW)
        assert ex.ledger_state("idem-1") is None
        node.finalize_complete = True
        assert (await ex.execute(_plan(), NOW)).txid == _RAW_TXID


def test_wallet_name_and_constructor_validation() -> None:
    client = httpx2.AsyncClient()
    with pytest.raises(ValueError, match="Wallet name"):
        LocalWalletRpc(client, 18443, "../x")
    with pytest.raises(ValueError, match="Unknown"):
        UtxoPsbtExecutor(LocalWalletRpc(client, 18443), "nope", _signer(), "w1")
    with pytest.raises(ValueError, match="fee rate"):
        UtxoPsbtExecutor(
            LocalWalletRpc(client, 18443),
            "btc-balance",
            _signer(),
            "w1",
            fee_rate_coins_per_kvb=Decimal(0),
        )


# --------------------------------------------------------------------------
# Real regtest node (skips without HCS_BITCOIND / HCS_LITECOIND / HCS_DIGIBYTED)
# --------------------------------------------------------------------------
_NODES = [
    ("HCS_BITCOIND", "btc-balance", "bitcoin:regtest", "2.0"),
    ("HCS_LITECOIND", "ltc-balance", "litecoin:regtest", "1.0"),
    ("HCS_DIGIBYTED", "dgb-balance", "digibyte:regtest", "1.0"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.regtest
@pytest.mark.anyio
@pytest.mark.parametrize(("env", "network_key", "chain", "version"), _NODES)
async def test_real_regtest_node_spend(
    env: str,
    network_key: str,
    chain: str,
    version: Literal["1.0", "2.0"],
    tmp_path: Path,
) -> None:
    binary = os.environ.get(env)
    if not binary or not shutil.which(binary):
        pytest.skip(f"{env} not set to a node binary")
    rpc_port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [
            binary,
            "-regtest",
            f"-datadir={tmp_path}",
            "-server=1",
            "-listen=0",
            "-rpcuser=hcs",
            "-rpcpassword=hcs-regtest-only",
            f"-rpcport={rpc_port}",
            f"-port={_free_port()}",
            "-fallbackfee=0.0002",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx2.AsyncClient(auth=("hcs", "hcs-regtest-only")) as client:
            root = LocalWalletRpc(client, rpc_port)
            for _ in range(120):
                try:
                    await root.call("getblockchaininfo", [], version=version)
                    break
                except Exception:  # node still starting
                    time.sleep(0.5)
            else:
                pytest.fail("regtest node did not start")
            try:
                await root.call("createwallet", ["w1"], version=version)
            except Exception:
                await root.call("loadwallet", ["w1"], version=version)
            wallet = LocalWalletRpc(client, rpc_port, "w1")

            async def w(method: str, *params: object) -> Any:
                return await wallet.call(method, list(params), version=version)

            mine = await w("getnewaddress")
            dest = await w("getnewaddress")  # own address is a valid regtest payee
            await w("generatetoaddress", 101, mine)
            plan = ValidatedTransactionPlan(
                canonical_chain_id=chain,
                network="regtest",
                wallet_id="w1",
                signer_address="node-wallet",
                asset_in="native",
                asset_out="native",
                amount_in_base_units=AMOUNT,
                min_amount_out_base_units=AMOUNT,
                destination_id=dest,
                max_slippage_bps=0,
                max_network_fee_base_units=1_000_000,
                max_priority_fee_base_units=0,
                idempotency_key="regtest-1",
                risk_decision_id="risk-1",
                risk_policy_version="p1",
                simulation_success=True,
                simulation_block_reference="tip",
            ).with_hash()
            signer = IsolatedSigner(
                SignerPolicy(
                    frozenset({chain}),
                    frozenset({dest}),
                    frozenset(),
                    0,
                    1_000_000,
                    live_mode_enabled=True,
                ),
                pause_state=lambda: False,
                risk_decision_lookup=lambda _decision_id: None,
            )
            result = await UtxoPsbtExecutor(wallet, network_key, signer, "w1").execute(
                plan, NOW
            )
            await w("generatetoaddress", 1, mine)
            tx = await w("gettransaction", result.txid)
            assert isinstance(tx, dict) and tx["confirmations"] >= 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _ambiguous(node: FakeNode, c: httpx2.AsyncClient) -> UtxoPsbtExecutor:
    ex = _executor(c)
    node.send_fails = True
    with pytest.raises(Exception, match="request failed"):
        await ex.execute(_plan(), NOW)
    node.send_fails = False
    return ex


@pytest.mark.anyio
async def test_deadline_is_rechecked_with_current_time_at_broadcast() -> None:
    node = FakeNode()
    plan = dataclasses.replace(
        _plan(), deadline=NOW + timedelta(seconds=30)
    ).with_hash()
    times = iter([NOW + timedelta(seconds=60)])
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c, clock=lambda: next(times))
        with pytest.raises(SignerPolicyViolation, match="expired"):
            await ex.execute(plan, NOW)
        assert "sendrawtransaction" not in node.calls
        assert ex.ledger_state(plan.idempotency_key) is None


@pytest.mark.anyio
async def test_deadline_still_valid_at_broadcast_succeeds() -> None:
    node = FakeNode()
    plan = dataclasses.replace(
        _plan(), deadline=NOW + timedelta(seconds=30)
    ).with_hash()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c, clock=lambda: NOW + timedelta(seconds=5))
        result = await ex.execute(plan, NOW)
    assert result.signed.signed_at == NOW + timedelta(seconds=5)


@pytest.mark.anyio
async def test_reconcile_unknown_key_and_completed_noop() -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c)
        assert await ex.reconcile("missing") == "UNKNOWN"
        await ex.execute(_plan(), NOW)
        assert await ex.reconcile("idem-1") == "COMPLETED"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tx",
    [
        {"txid": _RAW_TXID, "confirmations": -1},
        {"txid": _RAW_TXID, "confirmations": 0, "abandoned": True},
        {"txid": _RAW_TXID, "confirmations": 0, "walletconflicts": ["aa" * 32]},
        {"txid": _RAW_TXID},
    ],
)
async def test_reconcile_conflicted_is_terminal_and_never_retries(
    tx: dict[str, Any],
) -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = await _ambiguous(node, c)
        node.wallet_tx = tx
        assert await ex.reconcile("idem-1") == "CONFLICTED"
        assert await ex.reconcile("idem-1") == "CONFLICTED"
        with pytest.raises(UtxoReconciliationRequired, match="CONFLICTED"):
            await ex.execute(_plan(), NOW)
        assert node.calls.count("sendrawtransaction") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tx",
    [
        {"txid": "11" * 32, "confirmations": 1},
        {"txid": _RAW_TXID, "confirmations": 1, "details": []},
        {
            "txid": _RAW_TXID,
            "confirmations": 1,
            "details": [
                {"address": DEST, "category": "send", "amount": -_coins(AMOUNT + 1)}
            ],
        },
        {
            "txid": _RAW_TXID,
            "confirmations": 1,
            "details": [{"address": DEST, "category": "send", "amount": "x"}],
        },
    ],
)
async def test_reconcile_rejects_wrong_or_mismatched_transaction(
    tx: dict[str, Any],
) -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = await _ambiguous(node, c)
        node.wallet_tx = tx
        with pytest.raises(UtxoReconciliationRequired):
            await ex.reconcile("idem-1")
        assert ex.ledger_state("idem-1") == "RECONCILIATION_REQUIRED"


@pytest.mark.anyio
async def test_reconcile_confirmed_payment_stores_result() -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = await _ambiguous(node, c)
        node.wallet_tx = {**node.wallet_tx, "confirmations": 3}
        assert await ex.reconcile("idem-1") == "COMPLETED"
        again = await ex.execute(_plan(), NOW)
        assert again.txid == _RAW_TXID
        assert again.change_base_units == INPUT - AMOUNT - 1_000


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [
        {"wallet_id": "other"},
        {"asset_out": "bitcoin:regtest/native:OTHER"},
        {"min_amount_out_base_units": AMOUNT - 1},
    ],
)
async def test_plan_field_mismatches_rejected_before_rpc(
    changes: dict[str, Any],
) -> None:
    node = FakeNode()
    plan = dataclasses.replace(_plan(), **changes).with_hash()
    with pytest.raises(SignerPolicyViolation):
        await _run(node, plan)
    assert node.calls == []


@pytest.mark.anyio
async def test_too_many_inputs_duplicate_outpoints_and_bad_amounts_rejected() -> None:
    node = FakeNode()
    node.built["tx"]["vin"] = [{"txid": _PREV, "vout": i} for i in range(21)]
    node.built["inputs"] = [{"witness_utxo": {"amount": _coins(INPUT)}}] * 21
    with pytest.raises(SignerTamperingError, match="number of inputs"):
        await _run(node)
    node = FakeNode()
    node.built["tx"]["vin"] = [{"txid": _PREV, "vout": 0}] * 2
    node.built["inputs"] = [{"witness_utxo": {"amount": _coins(INPUT)}}] * 2
    with pytest.raises(SignerTamperingError, match="twice"):
        await _run(node)
    for bad in ("1.0", True, 0.123456789):
        node = FakeNode()
        node.built["inputs"] = [{"witness_utxo": {"amount": bad}}]
        with pytest.raises(SignerTamperingError, match="coin amount"):
            await _run(node)


@pytest.mark.anyio
async def test_broadcast_txid_mismatch_requires_reconciliation() -> None:
    node = FakeNode()
    node.sent_txid = "99" * 32
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c)
        with pytest.raises(SignerTamperingError, match="Broadcast txid"):
            await ex.execute(_plan(), NOW)
        assert ex.ledger_state("idem-1") == "RECONCILIATION_REQUIRED"


@pytest.mark.anyio
async def test_expected_genesis_mismatch_disables_spend() -> None:
    node = FakeNode()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(node.handle)) as c:
        ex = _executor(c, expected_genesis="11" * 32)
        with pytest.raises(UtxoSpendDisabled, match="genesis"):
            await ex.execute(_plan(), NOW)
        ok = _executor(c, expected_genesis="00" * 32)
        assert (await ok.execute(_plan(key="idem-9"), NOW)).txid == _RAW_TXID
    assert "sendrawtransaction" in node.calls
