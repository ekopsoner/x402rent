"""solrent402 — pay-per-call Solana empty-ATA rent recovery for AI agents.

Given a wallet, returns a serialized UNSIGNED Solana transaction that closes
every empty Associated Token Account owned by that wallet, reclaiming
~0.002 SOL of rent per closed account. Wallet signs and submits itself —
service never holds keys.

Paid via x402 micropayment on Solana mainnet; facilitator is PayAI.
"""
import logging
import os
from contextlib import asynccontextmanager

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from solders.pubkey import Pubkey
from x402 import x402ResourceServer
from x402.extensions.bazaar.resource_service import (
    OutputConfig,
    declare_discovery_extension,
)
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.svm.exact import register_exact_svm_server

from lib.rent_closer import (
    LAMPORTS_PER_SOL,
    MAX_CLOSES_PER_TX,
    RENT_LAMPORTS_PER_TOKEN_ACCOUNT,
    build_close_tx,
    find_empty_atas,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("solrent402")

PAY_TO          = os.environ["SOLRENT_PAY_TO"]
NETWORK         = os.environ.get("SOLRENT_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
FACILITATOR_URL = os.environ.get("SOLRENT_FACILITATOR", "https://facilitator.payai.network")

HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "").strip()
HELIUS_RPC_URL  = os.environ.get("HELIUS_RPC_URL") or (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
)

PRICE_PER_CALL = os.environ.get("SOLRENT_PRICE", "$0.05")

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
x402_server = x402ResourceServer(facilitator)
register_exact_svm_server(x402_server, networks=NETWORK)


CLOSE_BAZAAR = declare_discovery_extension(
    input={"wallet": "Aa1bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMmNnOoPpQqRrSsTtUu"},
    input_schema={
        "type": "object",
        "properties": {
            "wallet": {
                "type": "string",
                "description": "Solana wallet address (base58, 32-44 chars) whose empty ATAs should be closed. Wallet must sign and submit the returned transaction.",
                "minLength": 32,
                "maxLength": 44,
            },
        },
        "required": ["wallet"],
    },
    output=OutputConfig(
        example={
            "wallet": "Aa1bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMmNnOoPpQqRrSsTtUu",
            "n_empty_atas_found": 47,
            "n_in_returned_tx": 25,
            "sol_recoverable_lamports": 50982000,
            "sol_recoverable": 0.050982,
            "fee_payer": "wallet itself",
            "rent_recipient": "wallet itself",
            "tx_base64": "AQAAAAAAAAAAAAAA... (unsigned, ready for wallet to sign)",
            "recent_blockhash": "9zZqfL...",
            "note": "Call again to drain remaining empty ATAs if n_empty_atas_found > n_in_returned_tx.",
        }
    ),
)

ROUTES: dict[str, RouteConfig] = {
    "GET /close-empty-atas": RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=PAY_TO,
            price=PRICE_PER_CALL,
            network=NETWORK,
            max_timeout_seconds=300,
        ),
        description=(
            "Solana empty-ATA rent recovery. Given a wallet, returns an "
            "unsigned transaction that closes every empty SPL Token Program "
            "account owned by that wallet, reclaiming ~0.002 SOL of rent per "
            "account. Wallet signs and submits — service holds no keys."
        ),
        mime_type="application/json",
        extensions=CLOSE_BAZAAR,
    ),
}


_session: aiohttp.ClientSession | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _session
    _session = aiohttp.ClientSession()
    yield
    await _session.close()


app = FastAPI(
    title="solrent402",
    description=(
        "Pay-per-call Solana empty-ATA rent recovery for AI agents and trading "
        "bots. Returns an unsigned transaction the wallet signs itself — no key "
        "custody. Accepts x402 USDC micropayments on Solana mainnet via PayAI."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def x402_paywall(request, call_next):
    return await payment_middleware(ROUTES, x402_server)(request, call_next)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "solrent402",
        "version": "0.1.0",
        "x402_version": 2,
        "network": NETWORK,
        "facilitator": FACILITATOR_URL,
        "pay_to": PAY_TO,
        "endpoints": {
            "/close-empty-atas": {
                "price_usd": float(PRICE_PER_CALL.lstrip("$")),
                "params": ["wallet (query)"],
                "scope": "SPL Token Program empty ATAs; Token-2022 not in v0",
                "returns": "unsigned base64 Solana transaction for wallet to sign",
                "max_closes_per_call": MAX_CLOSES_PER_TX,
            }
        },
    }


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "helius_configured": bool(HELIUS_RPC_URL)}


@app.get("/close-empty-atas")
async def close_empty_atas(
    wallet: str = Query(..., description="Solana wallet address (base58)"),
):
    if not HELIUS_RPC_URL:
        raise HTTPException(500, "server not configured: HELIUS_API_KEY missing")
    try:
        Pubkey.from_string(wallet)
    except Exception:
        raise HTTPException(400, f"invalid wallet address: {wallet}")

    try:
        empty = await find_empty_atas(_session, HELIUS_RPC_URL, wallet)
    except Exception as e:
        log.warning(f"find_empty_atas failed for {wallet}: {e}")
        raise HTTPException(502, "upstream Helius RPC error")

    if not empty:
        return {
            "wallet": wallet,
            "n_empty_atas_found": 0,
            "n_in_returned_tx": 0,
            "sol_recoverable_lamports": 0,
            "sol_recoverable": 0.0,
            "tx_base64": None,
            "recent_blockhash": None,
            "note": "No empty Token Program ATAs found for this wallet.",
        }

    batch = empty[:MAX_CLOSES_PER_TX]
    try:
        tx_b64, blockhash = await build_close_tx(_session, HELIUS_RPC_URL, wallet, batch)
    except Exception as e:
        log.warning(f"build_close_tx failed for {wallet}: {e}")
        raise HTTPException(502, "failed to build close transaction")

    sol_recoverable_lamports = len(batch) * RENT_LAMPORTS_PER_TOKEN_ACCOUNT
    return {
        "wallet":                   wallet,
        "n_empty_atas_found":       len(empty),
        "n_in_returned_tx":         len(batch),
        "sol_recoverable_lamports": sol_recoverable_lamports,
        "sol_recoverable":          sol_recoverable_lamports / LAMPORTS_PER_SOL,
        "fee_payer":                wallet,
        "rent_recipient":           wallet,
        "tx_base64":                tx_b64,
        "recent_blockhash":         blockhash,
        "note": (
            f"Call again to drain remaining empty ATAs if n_empty_atas_found > "
            f"n_in_returned_tx (capped at {MAX_CLOSES_PER_TX}/tx for transaction "
            f"size limits). Submit returned tx after signing with wallet key."
        ),
    }
