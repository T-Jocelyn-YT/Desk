# Desk

Paper-trading terminal for Polymarket's 15-minute Ethereum Up or Down markets (series `eth-up-or-down-15m`, window slug `eth-updown-15m-<unix start>`). It reads the order book through Polymarket's official `py-clob-client` in read-only mode. It does not place live orders.

Live trading is a flag that this build refuses. `MODE` defaults to `paper`. `--live` and `MODE=live` exit without sending an order.

## Install

Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` if you want the placeholders in the environment. The file has `MODE=paper` and log paths. It has no credential fields. The process never reads a private key, seed phrase, or API secret, and paper mode runs without one.

## Tests

```bash
python -m pytest
```

The tests use fixtures and mocks. They do not require a network connection.

## Run paper mode

```bash
python -m desk
```

One pass, then exit:

```bash
python -m desk --once
```

On startup the process:

1. Stops immediately when a `FREEZE` file is present (path override: `DESK_FREEZE_FILE`). Exit code 3. A daily summary is written first.
2. Calls `https://polymarket.com/api/geoblock`. If `blocked` is true, or the check does not return a usable `blocked: false`, the process refuses to run. Exit code 2. The detected IP is not printed or logged.
3. Loads the current 15-minute ETH up/down event from Gamma (rules text, `cryptoMarketConfig`, `feeSchedule`, `orderMinSize`, `eventMetadata`) and the CLOB market (`fd`, `mos`) plus the order book minimum from `py-clob-client`.
4. Subscribes to the public RTDS socket `wss://ws-live-data.polymarket.com`, topic `crypto_prices_twap_sixty`, symbol `eth/usd`. Fair odds use `full_accuracy_value` (E18 integer string). The socket is public. This build does not use the credentialed PolyBolt socket. The socket reader already reconnects on its own.

Gamma, CLOB, geoblock, and order-book reads retry a dropped SSL connection or a URL disconnect. The policy is three attempts, with 0.2s and then 0.5s between them. An HTTP response (including 4xx and 5xx), a certificate failure, a geoblock refusal, a `FREEZE` file, and a request to run live mode are not retried. If the attempts are used up on a Gamma, CLOB, or order-book read, the process prints the error class and exits 6. A geoblock check that still fails is a refusal, exit 2. Neither path prints a traceback, a URL, or an address. The public RTDS loop is unchanged.

Logs (directory override: `DESK_LOG_DIR`, default `logs/`):

| File | Contents |
| --- | --- |
| `fills.jsonl` | Every paper fill, then a settlement line when that window resolves |
| `heartbeat.jsonl` | One line at start and every 60 seconds |
| `daily-summary.jsonl` | UTC day roll, shutdown, and FREEZE |
| `startup.jsonl` | Fee formula and minimum size read at startup |

A settlement line is appended to `fills.jsonl` when a bought window resolves. It records the outcome (`Up` or `Down`), the open and final prices the desk settled on, and that fill's realized P&L after fees. Up wins when the final price is greater than or equal to the open, including a tie. The final price is Gamma `finalPrice` when that official print is already present, otherwise the first public 60-second TWAP tick in the 5 seconds at the window end. The open is the same price used for fair odds. The line is not written before those prices exist.

Ctrl-C writes the daily summary and exits.

## What it trades

Only markets that pass every one of these checks:

- Slug `eth-updown-15m-<unix>` or `eth-up-or-down-15m-<unix>`
- Series `eth-up-or-down-15m` when Gamma sends a series slug
- `cryptoMarketConfig` asset `eth`, duration `15m`, TWAP enabled, 60-second lookback
- Resolution source `https://data.chain.link/streams/eth-usd-twap-60s-streams`
- Outcomes Up and Down
- A non-empty rules description fetched with the event

Other markets are ignored.

## Fee

The taker fee is the protocol curve, with the numbers taken from the payload:

```text
fee = shares × rate × (price × (1 − price)) ^ exponent
```

`rate` and `exponent` are read from CLOB `fd.r` and `fd.e`. Gamma `feeSchedule.rate` and `feeSchedule.exponent` must match. The legacy `tbf` / `base_fee` field is not the curve coefficient and is not used. USDC fees are rounded to 5 decimal places; a rounded fee below 0.00001 is zero.

Polymarket's docs describe the crypto coefficient as 0.07 until 2026-11-21 07:59 Asia/Shanghai (2026-11-20 23:59 UTC), and 0.01 for ETH 15-minute markets after that. This program does not switch coefficients on that clock. It uses whatever the startup read returns, including a later change. If Gamma and the CLOB disagree, it does not trade.

## Fair odds

Watcher v1, settlement on the Chainlink ETH/USD 60-second TWAP. Up when the TWAP at the end of the window is greater than or equal to the TWAP at the start. Ties resolve Up.

```text
P(Up) = Φ( [ln(S_t / S_0) − ½ σ² τ] / (σ √τ) )
```

`S_t` is the latest public TWAP tick. `τ` is seconds remaining / 900. Drift is zero. `σ` is the sample standard deviation of official 15-minute log returns `ln(finalPrice / priceToBeat)` from the last 16 settled windows. The last 95 windows are computed as a check and are not used for the quote. Down is `1 − P(Up)`.

`S_0` is chosen in this order, and the loop does not wait:

1. Gamma `eventMetadata.priceToBeat` on the current window
2. Gamma `eventMetadata.finalPrice` on the previous window
3. The first public TWAP tick in the 5 seconds at the window boundary

If none of those is available yet, there is no fair price and the desk does not buy. A mid-window tick updates `S_t` only. It is not treated as the open.

The 6-point rule is `edge = fair − fee_per_share − book_price`. Buy only when `edge >= 0.06`.

## Hard limits

These are constants in `src/desk/limits.py`. The code enforces them. Display config cannot widen them.

| Rule | Value |
| --- | --- |
| Bankroll | $50 starting cash. A buy whose cost exceeds remaining cash or $50 is rejected. |
| Per bet | 2% ($1), raised to the exchange minimum share size read at startup |
| Per window | 10% ($5) of notional in that 15-minute window |
| Daily stop | 6% ($3). No new buy whose worst case (notional + fee), together with open worst case and realized P&L, would push the UTC day through −$3. The stop clears on the next UTC day. |
| Price ceiling | 72¢. A higher ask is rejected. 72¢ is allowed by this rule. |
| Clock | Buy only in the last 4 minutes of the window (240 seconds remaining through 1 second remaining) |
| Edge | Fee-adjusted fair odds must beat the ask by at least 6 points |

A 5-share buy at 72¢ is about $3.60 before fees. That price passes the ceiling and the $5 window cap, and it fails the $3 daily stop, so the desk does not place it. A 5-share buy at 50¢ is $2.50 before fees and can pass on a fresh day. The share minimum is the value read from Gamma `orderMinSize`, CLOB `mos`, and the order book's `min_order_size`. Those three must agree. The desk does not assume the minimum stays at 5 shares.

## Paper fills

A buy lifts the current best ask and nothing behind it. Size is the smaller of the sized order and the shares displayed at that price. If that size is below the exchange minimum, there is no fill. The taker fee from the startup schedule is charged. Nothing is posted to the CLOB.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean shutdown |
| 2 | Geoblock refusal |
| 3 | FREEZE file |
| 4 | Live mode or a credential flag was requested |
| 5 | Market rules, fee, or minimum size could not be read |
| 6 | A network read still failed after the SSL/URL retries |

This repository does not deploy the terminal, provision a server, or turn live trading on.
