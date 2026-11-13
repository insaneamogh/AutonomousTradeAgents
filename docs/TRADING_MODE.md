# Paper vs real-money trading: the runtime switch

An admin can switch the platform between paper and real-money trading
without a redeploy. Code: `apps/api/app/services/platform/trading_mode.py`
(`live_trading_state()` is the single source of truth),
`apps/api/app/routers/admin.py`, migration `0022_platform_settings`.

## Three keys for a real order

A real-money order is placed only when ALL of these hold; any one missing
refuses with the named rule `live_trading_disabled`
(`services/orders/live_trading_gate.py`):

1. **The platform live switch is on**: the admin switch, or the legacy env
   `LIVE_TRADING_ENABLED=1`.
2. **The connection's own consent**: `live_trading_consent`, granted by its
   owner in the app.
3. **A live connection**: a non-paper broker connection (Alpaca live, all
   of Zerodha). Paper connections never reach this gate.

## Resolution of key 1

| `LIVE_TRADING_ENABLED` | Result |
|---|---|
| truthy (`1/true/yes/on`) | live, source `env` (legacy behaviour; the admin cannot turn it off) |
| `0/false/no/off`, or any unrecognised value | paper, source `env`, **locked**: a deploy-level lock the admin switch cannot override (POST returns 409) |
| unset or empty | the stored admin value; default paper |
| any error reading the store | paper (fails closed, logged) |

To make real money impossible at deploy level, set `LIVE_TRADING_ENABLED=0`.

## Admin identity

`ADMIN_EMAILS` is a comma-separated, case-insensitive list. Unset means
nobody is an admin. Admin requires a real signed-in session; the dev bypass
and demo sessions are never admin.

## API

- `GET /api/v1/admin/trading-mode` (any real session):
  `{mode, source, locked, isAdmin, updatedBy, updatedAt}`
- `POST /api/v1/admin/trading-mode` (admin): `{mode: "paper"|"live", confirm?}`.
  Going live needs `confirm == "ENABLE LIVE TRADING"` exactly (400 otherwise).
  Going back to paper needs no phrase. 403 non-admin, 409 env-locked, 503 if
  the setting could not be saved (nothing changes).

Every change is logged at WARNING with the admin's email, raises an ops
alert (best effort), and appends a row to `platform_setting_audit`.

## What the switch does NOT open

- **Auto-approve stays paper-only by construction.** Gate 2 of
  `auto_approver.py` refuses when the resolver says live, for everyone, even
  with `AUTO_APPROVE_ENABLED=1` and per-user consent. Live orders are always
  a human click.
- **The options agents' tools stay paper-only.** They refuse when the
  environment's Alpaca endpoint is live (`ab2b839ef`); that guard reads the
  endpoint, not this switch, and was deliberately left unchanged.
