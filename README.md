# PocketTrader — signal assistant

A Telegram bot that watches crypto pairs, runs four indicator strategies over
each closed candle, and tells you when they agree on a direction. Access is sold
through Crypto Pay (@CryptoBot), in crypto, inside Telegram.

It produces **opinions, not orders**: nothing here connects to a broker and
nothing places a trade.

> **Read this before you use it.** Signals are indicator output, not advice.
> Short-expiry binary options pay out less than they cost in expectation at
> every broker's published payout — a 50/50 outcome paying 80% loses money over
> time, so a strategy has to be genuinely better than a coin flip just to break
> even. Treat the output as one input among several, and never size a position
> you cannot afford to lose. Binary options are restricted or banned for retail
> clients in the EU, UK and several other jurisdictions; check your own rules.

## What it does

- **Four strategies vote on every bar.** `ema_pullback` (trend continuation),
  `bb_reversion` (fade a stretched move), `macd_momentum` (impulse with the
  trend) and `stoch_cross` (reversal out of an extreme).
- **The engine blends the votes.** Confidence is the net *weighted* agreement:
  agreeing strength minus opposing strength over the whole panel. One strategy
  shouting into three silent ones scores low, by design.
- **Volatility gates.** A bar whose ATR is above the ceiling or below the floor
  is thrown out before a direction is ever reported.
- **Closed candles only.** The in-progress candle is dropped, so a signal does
  not flicker within its own period.
- **Push alerts.** Opt in and the bot scans your watchlist on a timer, messaging
  you when something clears your personal threshold. Repeats are suppressed for
  a cooldown.
- **Billing.** Crypto Pay invoices, a signed webhook that credits access the
  moment payment clears, and a polling fallback for when the webhook is lost.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in TELEGRAM_BOT_TOKEN
set -a; . ./.env; set +a

python -m pockettrader scan                 # one-off scan of the watchlist
python -m pockettrader signal BTC/USDT      # one pair, with every vote shown
python -m pockettrader config               # the resolved configuration
python -m pockettrader bot                  # run the Telegram bot
```

With `CRYPTOBOT_TOKEN` empty, billing is off and every chat has full access —
convenient locally, wrong in production.

## Telegram commands

| Command | What it does |
| --- | --- |
| `/signal BTC/USDT [5m]` | Analyse one pair now, with each strategy's reasoning |
| `/scan [5m]` | Run the whole watchlist |
| `/watch`, `/watch add BTC/USDT`, `/watch remove BTC/USDT`, `/watch reset` | Manage your watchlist (max 25) |
| `/alerts on\|off` | Push new setups as they appear |
| `/settings tf 15m`, `/settings minconf 0.65` | Your timeframe and confidence floor |
| `/status` | Access, settings, watchlist |
| `/plans`, `/buy` | Purchase access through Crypto Pay |
| `/grant <chat_id> <days>` | Admin only, listed in `POCKETTRADER_ADMIN_CHAT_IDS` |

Direction is in option terms: **CALL** expects price up at expiry, **PUT** down.
Suggested expiry is `expiry_bars` candles of the timeframe you are on.

## Billing setup

Crypto Pay runs two separate networks, and an app belongs to exactly one of
them. Create one app per environment and keep the tokens apart.

**Local (testnet)**

1. Open [@CryptoTestnetBot](https://t.me/CryptoTestnetBot) → `/start`.
2. Crypto Pay → **Create App**, copy the API token into `CRYPTOBOT_TOKEN`.
3. Set `CRYPTOBOT_TESTNET=true`.
4. Expose the webhook port publicly — Crypto Pay only delivers to public HTTPS:
   ```bash
   cloudflared tunnel --url http://localhost:8080     # or ngrok http 8080
   ```
   Put the resulting origin in `PUBLIC_BASE_URL`.
5. In the app's settings: **Webhooks** → enable → paste
   `<PUBLIC_BASE_URL>/api/billing/webhook`.
   `python -m pockettrader config` prints the exact URL to paste.
6. Fund a test wallet from the same bot and buy a plan to check the flow.

**Production (mainnet)**

The same steps in [@CryptoBot](https://t.me/CryptoBot), with
`CRYPTOBOT_TESTNET=false`, the real domain in `PUBLIC_BASE_URL`, and its own
token in `.env.prod`. A testnet token on mainnet (or the reverse) fails at
startup with a clear log line — `getMe` is called on boot for exactly that
reason.

**How a payment lands**

1. `/buy` creates an invoice and stores it as pending.
2. The user pays; Crypto Pay POSTs to `/api/billing/webhook`.
3. The signature is checked — `HMAC-SHA256(body, SHA256(token))` — and anything
   unsigned or forged is refused with a 401.
4. The invoice is settled **once**: a replayed delivery cannot double-credit.
5. Access is extended and the user is messaged.

If the webhook never arrives (tunnel down, delivery lost), a poll every
`invoice_poll_seconds` checks pending invoices and settles them the same way,
through the same once-only path.

## Deployment

```bash
docker compose up -d --build
curl localhost:8080/health
```

`data/` holds `subscribers.json` — subscribers, settings and invoices. It is
mounted as a volume; back it up, because losing it means losing who has paid.

## Configuration

Every setting can come from a YAML file (`--config`, see
`config.example.yaml`) or from the environment, where `POCKETTRADER_*` wins over
the file. `TELEGRAM_BOT_TOKEN`, `CRYPTOBOT_TOKEN`, `CRYPTOBOT_TESTNET` and
`PUBLIC_BASE_URL` are also read under their bare names. An empty value counts as
unset, so a blank line in an env file cannot quietly flip you to mainnet.

| Setting | Default | Meaning |
| --- | --- | --- |
| `exchange` | `binance` | Any ccxt spot exchange id |
| `timeframe` | `5m` | `1m` … `1d` |
| `min_confidence` | `0.55` | Net weighted agreement needed |
| `min_votes` | `2` | Strategies that must agree |
| `expiry_bars` | `3` | Suggested expiry, in candles |
| `max_atr_pct` / `min_atr_pct` | `0.035` / `0.0005` | Volatility gates |
| `scan_interval_seconds` | `300` | Alert scan cadence |
| `alert_cooldown_seconds` | `1800` | Repeat suppression per signal |
| `rate_limit_per_minute` | `20` | Commands per **user**, not per IP |
| `free_trial_days` | `3` | Granted on first `/start` |

Rate limiting is keyed on the Telegram user id on purpose: users behind one
carrier NAT share an address, so an IP bucket punishes bystanders for one
person's spam.

## Adding a strategy

Subclass `Strategy`, return a `Vote` for the last closed bar, register it:

```python
# pockettrader/strategies/my_idea.py
class MyIdea(Strategy):
    name = "my_idea"
    min_bars = 120
    weight = 1.0

    def _vote(self, df):
        if some_condition(df):
            return self.vote(Direction.CALL, 0.7, "why it fired")
        return self.abstain("no setup")
```

Add it to `REGISTRY` in `pockettrader/strategies/__init__.py` and it becomes
selectable by name in `strategies:`. Strategies are pure functions of the frame
— no network, no state — which is what keeps them testable.

## Layout

```
pockettrader/
  indicators.py   EMA/RSI/MACD/Bollinger/ATR/Stochastic/ADX on plain pandas
  strategies/     one file per strategy, plus the registry
  engine.py       runs the panel, blends votes, applies the gates
  data.py         ccxt OHLCV with a TTL cache; drops the open candle
  models.py       Direction, Vote, Signal
  bot.py          Telegram handlers, the scan job, the invoice poll
  billing.py      Crypto Pay client and webhook signature check
  webhook.py      aiohttp endpoint + /health
  storage.py      JSON store: subscribers, settings, invoices
  ratelimit.py    per-user sliding window
  formatting.py   Telegram HTML and CLI rendering
  cli.py          scan / signal / bot / config
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Nothing in the suite touches an exchange or Telegram: market data is synthetic,
the Crypto Pay client runs against a mock transport, and the webhook runs
against an aiohttp test server. So it runs offline, with no tokens, in about a
second.

### Testing a payment without Crypto Pay

`tools/fake_payment.py` sends the bot a correctly signed `invoice_paid`, which
exercises the whole path — signature check, one-time settlement, the grant, the
message to the user — without creating an app or opening a tunnel.

```bash
export CRYPTOBOT_TOKEN=...                                  # same token the bot runs with
python tools/fake_payment.py --invoice 42 --chat 555 --plan week
python tools/fake_payment.py --invoice 42 --chat 555 --plan week   # again: no double credit
python tools/fake_payment.py --invoice 42 --chat 555 --unsigned    # expect 401
```

The invoice id comes from `/buy`, which stores it as pending.

## Roadmap

- Forex/OTC data adapter behind the existing `DataProvider` protocol
- Backtest harness to measure win rate per strategy at a given payout
- Per-strategy weights tuned from that backtest rather than set by hand
