"""rent_closer — find empty Solana ATAs for a wallet and build a tx to close them.

Returns an UNSIGNED Solana transaction (base64) that:
  - closes every empty SPL Token Program account owned by the wallet
  - uses the wallet itself as the fee payer AND the rent recipient

The service never touches private keys. The buyer's agent signs and submits
the returned transaction via its own Solana client.

Per-account rent recovery is the Associated Token Account exempt-min
(~0.00203928 SOL ≈ $0.40 at common SOL prices). A wallet with 100 dust ATAs
reclaims ~$40 of SOL.
"""
from __future__ import annotations

import base64
import logging

import aiohttp
from solders.hash import Hash
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import CloseAccountParams, close_account

log = logging.getLogger("solrent402.rent_closer")

# An empty signature (64 zero bytes) used to compute the serialized size of an
# unsigned transaction. Solders' Transaction requires signatures of the right
# count; using zero-signatures is the standard "unsigned-but-serializable" form.
from solders.signature import Signature
_ZERO_SIG = Signature.default()

# Max instructions per tx — Solana transactions cap at 1232 bytes. A
# close_account instruction is small (~37 bytes including header), so we can
# fit roughly 25-30 closes per tx with margin. Be conservative.
MAX_CLOSES_PER_TX = 25


async def _rpc(session: aiohttp.ClientSession, rpc_url: str, method: str, params: list):
    async with session.post(rpc_url, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
    return data.get("result")


async def find_empty_atas(
    session: aiohttp.ClientSession,
    rpc_url: str,
    wallet: str,
) -> list[str]:
    """Return list of token-account addresses owned by `wallet` that hold 0 tokens.

    Currently scopes to legacy SPL Token Program (Tokenkeg...). Token-2022 is
    a separate program and a separate listing call; not included in v0.
    """
    res = await _rpc(session, rpc_url, "getTokenAccountsByOwner", [
        wallet,
        {"programId": str(TOKEN_PROGRAM_ID)},
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ])
    if not res or "value" not in res:
        return []
    empty = []
    for item in res["value"]:
        try:
            info = item["account"]["data"]["parsed"]["info"]
            ui_amount = float(info["tokenAmount"]["uiAmount"] or 0)
            if ui_amount == 0:
                empty.append(item["pubkey"])
        except (KeyError, TypeError, ValueError):
            continue
    return empty


async def build_close_tx(
    session: aiohttp.ClientSession,
    rpc_url: str,
    wallet: str,
    accounts: list[str],
) -> tuple[str, str]:
    """Build an unsigned Solana transaction that closes the given token accounts.

    Returns (base64_tx, recent_blockhash). The wallet is the fee payer AND the
    rent recipient. Buyer must sign with the wallet's private key and submit.
    """
    if not accounts:
        raise ValueError("no accounts to close")

    wallet_pk = Pubkey.from_string(wallet)
    instructions = [
        close_account(CloseAccountParams(
            program_id=TOKEN_PROGRAM_ID,
            account=Pubkey.from_string(acc),
            dest=wallet_pk,
            owner=wallet_pk,
        ))
        for acc in accounts
    ]

    # Recent blockhash required for the message
    blockhash_res = await _rpc(session, rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    if not blockhash_res or "value" not in blockhash_res:
        raise RuntimeError("could not fetch recent blockhash")
    blockhash_str = blockhash_res["value"]["blockhash"]
    blockhash = Hash.from_string(blockhash_str)

    message = Message.new_with_blockhash(instructions, wallet_pk, blockhash)
    # Create unsigned tx: signatures slot present but zeroed; buyer fills in.
    tx = Transaction.new_unsigned(message)

    return base64.b64encode(bytes(tx)).decode("ascii"), blockhash_str


# Solana account rent for a standard token account = 2,039,280 lamports
# (SPL Token Program "AccountState" struct = 165 bytes, rent-exempt minimum
# at Solana's current rent rate). Used for the sol_recoverable summary.
RENT_LAMPORTS_PER_TOKEN_ACCOUNT = 2_039_280
LAMPORTS_PER_SOL = 1_000_000_000
