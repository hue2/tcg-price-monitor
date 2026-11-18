# Liquidity signal spike (B14, SPEC §9.4 item 4)

**Question:** does TCGCSV expose listing counts or sales volume we could use to suppress
noisy `mid` fallbacks on illiquid cards?

**Answer: no.** Checked 2026-11-06 with a few small GETs (Magic cat 1/group 1, Pokemon cat 3/group 24831).

## Fields actually returned

`/tcgplayer/{cat}/{group}/prices` (per productId + subTypeName):
`productId, subTypeName, lowPrice, midPrice, highPrice, marketPrice, directLowPrice`.
No quantity, listing count, seller count or sales count.

`/tcgplayer/{cat}/{group}/products`:
`productId, name, cleanName, imageUrl, categoryId, groupId, url, modifiedOn, imageCount,
presaleInfo, extendedData` (extendedData is card attributes only, e.g. Rarity, Number,
SubType, HP, CardText). Nothing liquidity-related. `modifiedOn` is catalog metadata, not a sales timestamp.

The daily 7z archive holds the same price rows (prices only), so history adds no volume either.
`/tcgplayer/{cat}/{group}` and `prices.csv` return 404.

## Usable proxies (no extra network calls)

Sample, Magic group 1: 776 price rows; 40 (5%) have `marketPrice` null; 547 (70%) have
`directLowPrice` null.

- `directLowPrice` null: weak hint of fewer Direct-eligible listings, but it's null for most rows, so too blunt.
- `high / low` spread ratio on the `mid` fallback: wide spread suggests thin/outlier listings.
- `marketPrice` null: already the trigger for the fallback.
- Flapping history: count of recent `mid` swings across stored snapshots (we have history locally).

## Implemented rule: `max_fallback_spread`

Since `min_listings` can't be computed from TCGCSV, `alert.max_fallback_spread` (default 10, on;
global or per game; `0`/`null` = off) skips a card when `marketPrice` is null, `mid` is used, and
`high / max(low, 0.01) > N`. Values must be numbers: non-numeric, bool, NaN, inf or negative values
are a `ConfigError` (CLI error / HTTP 400). `tcgwatch check` prints how many cards were skipped.

**Watchlist exception:** cards on the watchlist are compared by the watchlist path, which bypasses
the filters by design. A watched noisy card is therefore still reported (in the Watchlist section
and on Discord) even if the normal scan counted it in "skipped N"; that count refers to the normal
scan alone.

True listing counts/volume would need TCGplayer's authenticated API (not free, partner
access) or a different source; out of scope.
