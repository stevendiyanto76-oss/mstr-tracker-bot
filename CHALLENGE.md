# MSTR Live Thesis Challenge

This repository is the private accounting source for the public MSTR Live Thesis Challenge.
The challenge ledger is separate from the legacy portfolio ledger and remains in `prelaunch`
until the owner explicitly confirms the official starting capital and start policy.

## Owner Decisions Required Before Launch

1. Confirm the official starting capital and currency.
2. Confirm the official start timestamp.
3. Choose one start policy:
   - start empty and record all challenge trades from zero; or
   - include the legacy MSTR position with its verified quantity and average cost.

Do not publish live performance until all three decisions are confirmed.

## Data Model

- `data/challenge_config.json`: challenge status and official start metadata.
- `data/challenge_events.jsonl`: append-only private event ledger.
- `data/challenge_snapshots.jsonl`: timestamped portfolio and market observations.
- `data/public/challenge_*.json`: sanitized website exports.

Financial values are stored as decimal strings and replayed with Python `Decimal`. Private
Telegram identifiers stay inside each event's `private_source` object and are rejected by the
public exporter. Public transaction rows include derived post-event balances, but they do not
claim broker verification.

## Telegram Commands

```text
/challenge_status
/challenge_init USD 1000
/cash
/deposit USD 100
/withdraw USD 100
/fx_convert IDR 1600000 USD 100
/fee USD 1.25
/tax USD 2.50
/buy_mstr 1.5 85
/sell_mstr 0.5 100
/portofolio
/history 20
/undo CH-000002
/challenge_reset CONFIRM
```

After activation, MSTR buy, sell, portfolio, history, last, and challenge-formatted undo commands
route to the challenge ledger. Before activation, existing legacy portfolio behavior is unchanged.

## Local Operator Commands

```powershell
python portfolio.py challenge-status
python portfolio.py challenge-init USD 1000 --start-empty
python portfolio.py challenge-init USD 1000 --include-legacy-position
python portfolio.py challenge-set-status paused
python portfolio.py export-public
python portfolio.py validate-public
python portfolio.py validate
python -m unittest discover -s tests -q
```

The legacy-position option reads the verified legacy quantity and average cost. Never infer or
invent those values manually.

## Public Export Contract

The exporter writes only when non-volatile content changes. Every public file includes
`schema_version`; audit data includes the source commit and a deterministic public-ledger hash.
Unknown market or thesis inputs remain `null` or `DATA_INSUFFICIENT`. Unknown gates cannot pass.

The website Pages Function should read only these six sanitized files:

```text
challenge_overview.json
challenge_transactions.json
challenge_performance.json
challenge_thesis.json
challenge_audit.json
challenge_health.json
```

For a private GitHub repository, store a read-only fine-grained token as a server-side Pages
secret. Restrict it to repository contents read access. Never expose that token to browser code.

## Recovery

1. Pause mutations with `python portfolio.py challenge-set-status paused`.
2. Run `python portfolio.py validate` and inspect the first integrity error.
3. Restore the last known-good data commit if persistence was interrupted.
4. Re-run validation, public export, and all tests.
5. Resume only after the ledger, snapshots, and public files all validate.

Use `/undo CH-...` for a single incorrect event when replay remains valid. Use reset only for an
explicitly abandoned challenge era; reset preserves prior history and starts a new era.
