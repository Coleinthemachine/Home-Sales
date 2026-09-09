# Home Sales

Tracks residential sales of **$1,000,000+** in **Chester**, **Montgomery** and
**Delaware** counties, Pennsylvania, with **Radnor Township** broken out as its
own area. Collects weekly, deduplicates across sources, and serves a local web
dashboard.

```
python -m home_sales run      # collect
python -m home_sales serve    # dashboard at http://127.0.0.1:5000
```

## Status: sources need endpoints before first use

The collection, deduplication, filtering and dashboard code is complete and
tested. **The county endpoints in `config.toml` are placeholders** — every
source ships `enabled = false`, so a fresh `run` collects nothing until you wire
at least one up. That is deliberate: county GIS portals rename layers and fields
without notice, so the URLs are configuration, and `discover` finds the current
ones rather than shipping guesses that silently rot.

See [Wiring up a county](#wiring-up-a-county) — it takes a few minutes per county.

## Quick start

**1. Install** (Python 3.11+):

```bash
git clone https://github.com/Coleinthemachine/Home-Sales.git
cd Home-Sales
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**2. Confirm it runs.** This should print the source list and exit cleanly:

```bash
python -m home_sales sources
```

**3. Identify yourself.** In `config.toml`, set `user_agent` and `contact_email`
under `[fetching]` to something real. Public records offices are far more
tolerant of a crawler they can contact than an anonymous one.

**4. Wire up at least one county** — see the next section. Nothing is collected
until you do; every source ships `enabled = false`.

**5. Collect and view:**

```bash
python -m home_sales run
python -m home_sales serve      # then open http://127.0.0.1:5000
```

Step 4 is the only part that takes real effort, and it's a one-time setup.

## Wiring up a county

Each county publishes its parcel data as an ArcGIS service; you need to find the
current URL and tell the bot which columns hold price and sale date. `config.toml`
lists candidate starting points for all three counties — try each with
`discover` and keep whichever answers. Then walk down:

```bash
# 1. List services
python -m home_sales discover https://gis.example-county.org/arcgis/rest/services

# 2. List layers in a likely service
python -m home_sales discover https://gis.example-county.org/arcgis/rest/services/Parcels/FeatureServer

# 3. Inspect a layer -- prints its fields, a sample record, and a config block
python -m home_sales discover \
  https://gis.example-county.org/arcgis/rest/services/Parcels/FeatureServer/0 \
  --name chester_county --county Chester
```

Step 3 prints a ready-to-paste `[sources.chester_county]` block with its best
guess at which columns hold price, sale date, address, municipality and the
grantor/grantee. Check the guesses against the sample record it prints, paste
the block into `config.toml`, set `enabled = true`, then verify:

```bash
python -m home_sales run --source chester_county --dry-run
```

A dry run collects and filters but writes nothing, printing the first 20
matches. When those look right, drop `--dry-run`.

If a county publishes no ArcGIS service, the `html_table` source handles pages
that list transfers in a table — configure it with CSS selectors.

## How it works

```
sources ──▶ normalize ──▶ filter ──▶ dedupe/merge ──▶ SQLite ──▶ dashboard
```

**Normalization** collapses formatting differences so records can be compared:
`123 North Wayne Avenue`, `123 N. Wayne Ave.` and `123 N WAYNE AVE` all reduce to
one key. Prices parse from `$1,250,000`, `1.25M` or a bare number; dates from ISO,
US, prose and ArcGIS epoch-millisecond forms.

**Filtering** keeps a sale only if it clears the price floor, falls inside the
lookback window, and matches a configured area. Areas match on county,
municipality *or* ZIP, so a news article that names only "Villanova" still
places correctly. Areas listing municipalities or ZIPs are treated as
sub-county and matched first — a Radnor sale is labeled `Radnor Township`,
not `Delaware County`.

**Deduplication** treats the same property within `dedupe_window_days` (default
14) as one transaction, because sources report deed-recording and settlement
dates that differ by days. When two sources describe the same sale, the
higher-`rank` source wins on conflicting values and the other fills in the gaps
— so a county record supplies the authoritative price while a listing API
contributes bed/bath/square footage.

**Source health** is recorded every run and shown on the dashboard. A source
that breaks shows up as `BLOCKED`, `MISCONFIGURED` or `ERROR` with the reason
rather than silently returning nothing.

## Source types

| Type | Use for | Notes |
|---|---|---|
| `arcgis` | County parcel/deed layers | Primary source. Public, structured, authoritative on price and parties. |
| `json_api` | Licensed vendors (RentCast, ATTOM, …) | Generic — endpoint, auth header, pagination and field paths are all config. |
| `news_rss` | Local press "notable sales" | Heuristic extraction from prose. Rank it low. Incomplete by nature. |
| `html_table` | Sites publishing transfer tables | CSS-selector driven. Obeys robots.txt. |

Set API keys as environment variables (`RENTCAST_API_KEY`, `ATTOM_API_KEY`);
`config.toml` references them as `${VAR}` and a source refuses to run rather
than sending an unexpanded placeholder as a credential.

## On scraping Zillow, Redfin and Realtor.com

You asked about these, so to be direct: this bot does not scrape them, and the
code will refuse to.

Their `robots.txt` files and terms of service prohibit automated collection.
They also actively block bots, so any scraper built against them breaks
constantly and needs evasion techniques to keep working — which is where a
gray-area project turns into a clearly bad one. All requests here go through a
single fetcher that checks `robots.txt` and raises `RobotsDisallowed` on a
disallowed path; the run continues, and the dashboard shows that source as
`BLOCKED` with the reason.

You can still get the same coverage legitimately:

- **County records** (`arcgis`) are the authoritative source for what actually
  sold and for how much. Everything else is downstream of them.
- **Licensed APIs** (`json_api`) — RentCast, ATTOM and similar resell MLS-derived
  data with a license that permits programmatic use. This is the paid path to
  listing-site-quality detail.
- **Local press** (`news_rss`) catches high-end sales before they clear the
  recorder of deeds.

Setting `respect_robots = false` exists for endpoints you own or operate. Using
it against a third party is on you, and is not what this project is for.

## Weekly runs

`.github/workflows/weekly.yml` runs the collection every Monday, commits the
updated database back to the repo, and uploads a CSV artifact. Add
`RENTCAST_API_KEY` / `ATTOM_API_KEY` as repository secrets if you enable those
sources.

To run it on your own machine instead:

```cron
0 6 * * 1 cd /path/to/Home-Sales && .venv/bin/python -m home_sales run >> run.log 2>&1
```

The 45-day default lookback means a missed week (or county recording lag) leaves
no gap — overlapping runs re-find the same sales and dedupe them.

## Commands

| Command | Purpose |
|---|---|
| `run` | Collect from enabled sources. `--dry-run`, `--source NAME`, `--days N`, `--min-price N` |
| `serve` | Dashboard. `--host`, `--port` |
| `discover URL` | Inspect an ArcGIS endpoint and generate a config block |
| `sources` | List configured sources and their last-run status |
| `export` | CSV export. `-o FILE`, `--days N` |

The dashboard filters by area, date window, price floor and free-text search,
exports the current view as CSV, and exposes `/healthz` for monitoring.

## Tests

```bash
python -m pytest
```

Fully offline — sources are tested against canned payloads, and the robots.txt
enforcement is tested with a stubbed session.

## Caveats

- Prices are as reported. County data includes non-arm's-length transfers —
  intra-family transfers, deed corrections and $1 transfers — and some counties
  record the transfer-tax basis rather than the sale price.
- Recording lag runs days to weeks, so a sale appears some time after closing.
- The `news_rss` extraction is heuristic and will occasionally pull the list
  price or a neighboring figure instead of the sale price. That is why it ranks
  below county records, which overwrite it on merge.
