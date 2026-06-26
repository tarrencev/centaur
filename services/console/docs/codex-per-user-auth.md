# Per-user Codex authentication

Lets each user run Codex against **their own ChatGPT subscription token** instead
of a single shared key, so usage and limits attribute to the individual. A user
links once; the console stores their refresh token and keeps the access token
live server-side, so agents keep working while the user's machine is offline.

## How it works

In a DM the session principal **is** the user (`slack-user-<id>`). Linking grants
two static secrets **directly** to that principal:

1. a `token_broker` secret → `Authorization: Bearer <live token>` on `chatgpt.com`,
   backed by a per-user standalone `BrokerCredential` the refresh loop keeps fresh;
2. a `control_plane` secret → the user's `chatgpt-account-id` header on `chatgpt.com`.

Direct (principal) grants default to priority 100 and the shared `openai-codex`
infra-role grant to 0, so `Principal#suppressed_conflict_credentials` makes the
per-user secrets win for linked users while everyone else falls through to the
shared credential. No iron-proxy/api-rs change is involved — the egress proxy
already syncs the principal's effective grants.

The credential is keyed by principal (`foreign_id: codex-<principal fid>`), so
re-linking overwrites it rather than accumulating stale grants.

Verified Codex OAuth facts (from `openai/codex`): token endpoint
`https://auth.openai.com/oauth/token`, public PKCE client `app_EMoamEEZ73f0CkXaXp7hrann`
(no client_secret, no scope on refresh), rotating refresh tokens (the broker
persists each rotation), account id from the id_token claim
`https://api.openai.com/auth.chatgpt_account_id`.

## User flow

1. User runs `/connect-codex` in a **DM** with the bot.
2. The bot mints a single-use pairing token (15-min TTL) and DMs the steps.
3. User runs `codex login` locally (if needed), then
   `codex-link <cpt_token> --console-url <public console URL>`
   (`services/console/bin/codex-link`, stdlib-only). It reads
   `~/.codex/auth.json` and uploads `{refresh_token, account_id}`.
4. The console validates the token by minting once, grants the secrets, and the
   next agent session in that DM uses the user's token.

Only DMs are supported in v1; channel sessions keep the shared credential.

## Deploy / ops checklist

1. **Switch Codex to access_token mode globally** so every Codex sandbox egresses
   to `chatgpt.com` (linked and unlinked alike):

   ```yaml
   # values.yaml
   sandbox:
     codexAuthMode: access_token   # default is api_key
   ```

2. **Provision the shared fallback** `openai-codex` broker credential (existing
   out-of-band step, e.g. `centaur-perms broker create` / the console), so
   unlinked users still have Codex access.

3. **Give the slackbot console access.** When `console.enabled` and
   `slackbotv2.enabled`, the chart wires `CENTAUR_CONSOLE_URL` (in-cluster),
   `CONSOLE_API_KEY` (from the secret store), and widens the console NetworkPolicy
   to admit slackbotv2. You must:
   - add a `CONSOLE_API_KEY` entry (an operator `ApiKey`, `iak_…`) to the secret
     store under `<envPrefix>CONSOLE_API_KEY`;
   - set the internet-facing console URL for the local helper:
     ```yaml
     slackbotv2:
       consolePublicUrl: https://console.<your-domain>
     ```

4. **Distribute `codex-link`** (`services/console/bin/codex-link`) to users, or
   document fetching it. It needs only Python 3 (stdlib).

## Security notes

- Refresh tokens are encrypted at rest (`BrokerCredential.encrypts :refresh_token`);
  pairing tokens are stored hash-only (SHA-256), single-use, 15-min TTL.
- The pairing token authorizes exactly one principal binding (server-side
  selector), so the local helper holds nothing reusable.
- Egress rules are locked to `chatgpt.com`, so a leaked token can't be replayed
  elsewhere.
- A rejected refresh token leaves the credential `dead` and the wrapping secret
  non-deliverable; the helper reports it and the pairing token stays usable for a
  retry after `codex login`.

## Out of scope (v1)

- Channel sessions (would need per-(user,channel) principal isolation).
- Discord `/connect-codex` (Slack first).
- Confirm OpenAI ToS for server-side refresh of the first-party `codex-cli` client.
