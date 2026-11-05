# Paper Trading

A long-only paper trading bot for ten tokens on Binance spot. It runs hourly on GitHub Actions, keeps its ledger in this repo, and publishes a dashboard on GitHub Pages.

**Dashboard:** https://khawajayy.github.io/PaperTrading/

No real money is involved. The bot has no exchange keys and can't place real orders.

## Tokens

| Token | Tracks | Exposure |
|---|---|---|
| SOXLB | Direxion Semiconductor Bull 3X (SOXL) | +3x semiconductors |
| SOXSB | Direxion Semiconductor Bear 3X (SOXS) | −3x semiconductors |
| SNXXB | Tradr 2X Long Sandisk (SNXX) | +2x Sandisk |
| SNDKB | Sandisk (SNDK) | +1x |
| MVLLB | GraniteShares 2X Long Marvell (MVLL) | +2x Marvell |
| MUUB | Direxion Micron Bull 2X (MUU) | +2x Micron |
| KORUB | Direxion South Korea Bull 3X (KORU) | +3x Korea |
| INTCB | Intel (INTC) | +1x |
| XAUT | Tether Gold | Gold |
| PAXG | Paxos Gold | Gold |

## How a trade happens

Each token gets a score from −100 to +100, calculated on closed candles only.

1. **Indicators:** EMA trend on 1h, 4h and daily, plus RSI, MACD, ADX, 20-hour breakouts and volume.
2. **News, up to ±15:** Google News and Yahoo Finance headlines from the last 48 hours, with newer stories counting more. Bear token SOXSB reads semiconductor news in reverse.
3. **Macro, up to ±15:** VIX and the chip ETF SMH for equities; the dollar index, the 10-year yield and VIX for gold.

The bot buys at a score of 55 or higher, unless one of these blocks the trade:

- High-impact bad news from at least two publishers in the last 12 hours
- Within 60 minutes before or 30 minutes after a high-impact US economic release
- Within 48 hours of the company's earnings
- VIX at 35 or above (bullish equity tokens only)
- A wide spread, low 24h volume, or the token trading over 1.5% above its real share
- A price more than 3.5 ATR above EMA20
- The US market being closed for the weekend (equity tokens only)

It sells when any of these happens:

- The stop loss (2.5 ATR) or trailing stop (3 ATR, active after a 1R gain) is hit
- The score falls to −15 or lower
- The trend breaks on both the 1h and 4h views
- High-impact bad news arrives
- Earnings are within 24 hours
- A leveraged token has been held for 7 days, because daily-reset products lose value over time

## Risk limits

- 1% of equity at risk per trade, halved when the account is 10% below its peak
- Maximum position: 10% for 3x tokens, 12% for 2x, 20% for 1x stocks, 30% for gold
- At most 4 positions and 80% of equity invested
- At most 40% in bullish semiconductor tokens, and they can never be held alongside SOXSB
- Only one gold token at a time, since XAUT and PAXG are close to the same trade
- No new trades after a 3% loss in a day (UTC)
- A position can't exceed 1% of the token's 24h volume

All of these live in [config.json](config.json).

## How fills are simulated

- Buys fill at the live ask and sells at the live bid, plus slippage, with a 0.1% fee each way.
- Stops are replayed on 5-minute candles since the last run, so a late GitHub run doesn't skip one.
- If a candle gaps through a stop, the fill is at the candle's open, with three times the normal slippage.
- Binance's fees for these tokens may differ from 0.1%. Check them against your account tier.

## The record

Everything is in [docs/data](docs/data), and every run is a git commit, so the history can be audited.

| File | Contents |
|---|---|
| `ledger.csv` | Every cash movement: deposit, buy, sell, fee |
| `trades.csv` | Closed trades with P&L, R multiple and exit reason |
| `equity.csv` | Account value, drawdown and benchmark at each run |
| `state.json` | Cash and open positions |
| `dashboard.json` | The data behind the dashboard |

On each run the bot rebuilds cash and positions from the ledger. If they don't match its state, the run fails and nothing is saved.

## Backtest

Run on 5 November 2026, from 3 August to 5 November 2026, using only the indicators and risk limits, net of fees and slippage:

| Measure | Result |
|---|---|
| Return | **−12.2%** |
| Buy and hold, all ten tokens | −22.1% |
| Max drawdown | −18.5% |
| Trades | 210 |
| Win rate | 30% |
| Profit factor | 0.73 |

**These rules lost money in this test.** They lost less than buy and hold, but a profit factor below 1 means no edge was shown. The window is only three months because most of these tokens launched in mid-2026, and it isn't enough to judge the strategy either way. News, macro and event filters aren't included because their history isn't available. The rules weren't tuned to this result, since fitting them to so little data would only make the test look better.

```bash
python -m paper_trader.backtest
```

## Running it

It uses only the Python 3.12 standard library, so there's nothing to install.

```bash
python -m unittest discover -s tests -t .
```

```bash
python -m paper_trader.run
```

A local run writes to `docs/data`, so don't commit it unless you mean to add it to the record.

To reset the record, delete `docs/data` and run the workflow again.

## Known limits

- Headline scoring only matches keywords, so it can misread a story. For example, "Gold rises as dollar sinks" counts "sinks" against gold.
- GitHub can delay scheduled runs, so entries and signal exits can happen late, though stops are still replayed.
- Earnings dates come from Nasdaq and only appear once announced. If the date is blank, that filter can't protect the trade.
- Paper fills assume the order would have filled at the quoted price. Thin tokens such as MUUB and MVLLB may fill worse in real trading.
