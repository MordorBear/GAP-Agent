# GAP-Agent — SAM.gov Set-Aside Contract Fetching Agent

A command-line agent that searches the U.S. Government's
[SAM.gov](https://sam.gov) Opportunities API for **active federal contract
opportunities** that are set aside for the following small-business programs and
generates a clean, professional **HTML report** you can open in any browser.

Set-aside programs searched:

| Code(s) | Program |
|---|---|
| `WOSB`, `WOSBSS` | Women-Owned Small Business |
| `EDWOSB`, `EDWOSBSS` | Economically Disadvantaged Women-Owned Small Business |
| `SDVOSBC`, `SDVOSBS` | Service-Disabled Veteran-Owned Small Business |
| `8A`, `8AN` | 8(a) Business Development |

## What it does

1. Reads your `SAM_GOV_API_KEY` from the environment (or a `.env` file).
2. Queries the [SAM.gov Opportunities API v2](https://open.gsa.gov/api/get-opportunities-public-api/)
   for each set-aside program, handling pagination to retrieve **all** matches.
3. Filters to opportunities with an estimated / award value **greater than
   $10,000** (configurable).
4. De-duplicates, then sorts results by **NAICS category** and, within each
   category, by **dollar value (highest first)**.
5. Writes **`sam_gov_listings.html`** — a responsive report with a summary
   header, category navigation, and a color-coded card for every opportunity
   (title, solicitation #, set-aside type, NAICS, value, agency, office,
   response deadline, place of performance, and a direct link to SAM.gov).

## Getting a SAM.gov API key

1. Sign in / create an account at <https://sam.gov/profile/details>.
2. Under **System Accounts → API Key**, generate a personal public API key.
3. Copy the key — you'll put it in your `.env` file below.

## Installation

```bash
git clone https://github.com/MordorBear/GAP-Agent.git
cd GAP-Agent
pip install -r requirements.txt
```

## Setup

Copy the example environment file and add your key:

```bash
cp .env.example .env
# then edit .env and set:
#   SAM_GOV_API_KEY=your_real_key_here
```

> **Note:** `.env` is git-ignored so your key is never committed.

## Running the agent

Run with defaults (all set-asides, value > $10,000, last 365 days, active only):

```bash
python sam_gov_agent.py
```

### Command-line options

The agent runs on command with configurable flags:

```bash
python sam_gov_agent.py --help
```

| Flag | Description | Default |
|---|---|---|
| `-o`, `--output` | Path of the HTML report to generate | `sam_gov_listings.html` |
| `-m`, `--min-value` | Minimum estimated/award value (USD) | `10000` |
| `-d`, `--days` | How many days back to search (max 365) | `365` |
| `-s`, `--set-aside` | Limit to specific set-aside code(s); repeatable | all |
| `--include-all` | Include opportunities regardless of active status | off |
| `--exclude-unknown-value` | Exclude opportunities with no published value | off |
| `--api-key` | Pass the API key directly (env var preferred) | — |

**Examples**

```bash
# Only WOSB and EDWOSB, value over $50k, last 90 days
python sam_gov_agent.py -s WOSB -s EDWOSB -m 50000 -d 90

# Custom output location
python sam_gov_agent.py -o reports/opportunities.html
```

Environment variables (`SAM_MIN_VALUE`, `SAM_LOOKBACK_DAYS`,
`SAM_INCLUDE_UNKNOWN_VALUE`, `SAM_ACTIVE_ONLY`, `SAM_OUTPUT_FILE`) can also be
set in `.env`; command-line flags always take precedence.

## Output

The agent writes **`sam_gov_listings.html`** to the current directory (or the
`--output` path). Open it in any web browser to review, print, or share the
opportunities.

> Many active solicitations do not publish a firm dollar figure. By default
> these are still shown (value listed as "Not specified"). Use
> `--exclude-unknown-value` to hide them.

## Disclaimer

Data is sourced live from the SAM.gov Opportunities API and is provided for
informational purposes only. Always verify opportunity details directly on
[SAM.gov](https://sam.gov) before acting on them.
