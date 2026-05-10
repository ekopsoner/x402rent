# solrent402

Pay-per-call Solana empty-ATA rent recovery for AI agents and trading bots.

Given a wallet, returns an **unsigned** Solana transaction that closes every
empty Associated Token Account owned by that wallet, reclaiming ~0.002 SOL
of rent per account. The wallet signs and submits itself — the service
holds no keys.

## Endpoint

```
GET /close-empty-atas?wallet=<pubkey>
```

Returns 402 Payment Required on unpaid requests. Accepts Solana USDC
micropayments via the x402 protocol (PayAI facilitator, gasless for buyers).
Pricing: $0.05/call.

## What you get back

```json
{
  "wallet": "...",
  "n_empty_atas_found": 47,
  "n_in_returned_tx": 25,
  "sol_recoverable_lamports": 50982000,
  "sol_recoverable": 0.050982,
  "fee_payer": "<wallet>",
  "rent_recipient": "<wallet>",
  "tx_base64": "<unsigned tx for wallet to sign>",
  "recent_blockhash": "..."
}
```

Sign and submit the `tx_base64` via your Solana client. The wallet is both
fee payer and rent recipient.

## Scope / limits

- SPL Token Program (legacy) accounts only — Token-2022 not in v0
- Max 25 closes per returned tx (Solana 1232-byte tx size limit)
- Call again if `n_empty_atas_found > n_in_returned_tx` to drain remainder

## Powered by

- [PayAI Network](https://payai.network) — Solana-first x402 facilitator
- [Helius](https://helius.dev) — Solana RPC
