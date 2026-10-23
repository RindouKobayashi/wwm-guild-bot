# Portable saved-login API provider

This package runs under ordinary Python on Windows or Linux, including a
Raspberry Pi. Saved-login reuse has no mitmproxy, desktop Steam, game installation, Ghidra, Toolkit,
research-output or Windows DPAPI runtime dependency. The saved context is still
a private TEST-account game login, not anonymous access or a locally generated
API signature. It may replace a lingering session of that configured test account.

Automatic SDK-login renewal is now available through the headless Steam helper.
See [headless setup and Pi transfer](headless_steam/README.md). It needs .NET 8
and two additional private files; it authenticates directly with Steam servers.
The Windows end-to-end renewal and protected test-character lookup passed.

## What is established

The standalone package retrieved a token using only the exported test credential
and returned the exact test character through protected player lookup. Windows
offline tests passed, including a standalone-copy import with no research files.
Pinned dependencies were checked for Python 3.11 Linux ARM64 wheel availability.
An actual Pi login has not yet been run; no remote files have been changed.

The minute monitor observed a fixed API token accepted at 22:08 and rejected at
22:09 and 22:10 on October 18, 2026 (Singapore time). The same saved game login
then obtained a different accepted token. That demonstrates old-token rejection
while the saved login still works; it does not prove a permanent game credential
or distinguish every possible reason for API-token invalidation.

## One-time private setup

### Renew from the Windows test account

Run `maintenance/research/Get Test WWM API Token.cmd` with Steam signed into
the verified test account. After owned-character API validation succeeds, it
installs the fresh login in `.wwm-auth/context.json` and backs up the previous
context under `.wwm-auth/backups/`. These contain private plaintext credentials
and are Git-ignored. Failed login/validation preserves the existing context;
account/character changes and active portable refreshes are rejected.

Then run `.venv\Scripts\python.exe -m wwm_auth --fetch` to independently verify
reuse without Steam, SDK or MITM calls. Only after that passes, securely copy the
new context to the Pi using the commands below. Do not copy the DPAPI token file
or caches. The fresh launcher requires local Windows research inputs, whereas
the Pi runtime does not. Alternatively use the headless renewal setup linked above.
Immediate successful reuse does not establish how long the saved login lasts.

The prepared local file is `.wwm-auth/context.json`. **It contains a reusable
login credential in plaintext**, like a secret .env file; keep it private. It
contains only the verified test account's authentication/device/routing context,
test-character identity guards and game public key. All `.wwm-auth/` files are
Git-ignored. Never add them, the research captures or token caches to Git.

After reviewing, committing and pushing the runtime files, transfer this one
file to the Pi separately. In the Pi SSH terminal:

The Pi must pull the branch containing those commits. If you keep production on
Git `main`, merge your reviewed `dev` changes into `main` first. The root .env's
`GITHUB_BRANCH=main` is a separate bot environment selector: do not change it
just because development work uses the Git `dev` branch.

```sh
cd ~/Desktop/wwm-guild-bot
git pull
mkdir -p .wwm-auth
chmod 700 .wwm-auth
```

From Windows PowerShell in the bot repository:

```powershell
scp .wwm-auth/context.json smolsus@smolsus:~/Desktop/wwm-guild-bot/.wwm-auth/context.json
```

Then on the Pi:

```sh
cd ~/Desktop/wwm-guild-bot
chmod 600 .wwm-auth/context.json
bash "Setup WWM API Auth.sh"
.venv/bin/python -m wwm_auth --fetch
```

Setup installs only the small provider dependencies into `.venv`, then checks
configuration offline. `--fetch` explicitly makes one bounded login attempt and
protected lookup; it does not launch Discord. The CLI prints no token values.
On Linux the provider refuses a group/world-readable context file.

On Windows, install `requirements-auth.txt` into the bot `.venv`, then run
`Check WWM API Auth.cmd` for the offline check. Explicit live validation:

```powershell
.\.venv\Scripts\python.exe -m wwm_auth --fetch
```

## Enable in the bot after validation

Copy the settings from `wwm_auth/example.env` into each device's existing root
`.env`. These settings do not select the Discord test/production application;
keep the existing per-device bot/Activity configuration unchanged. Neither setup
nor this preparation edits your `.env`. Start the usual bot/Activity launcher.

`utility/wwm.py` uses the provider only when `WWM_AUTH_MODE=saved_login`. Without
that setting, existing behavior remains. Explicit `token=` arguments bypass it.
The provider caches validated tokens, performs at most one authorization-driven
refresh per API call, serializes refreshes across local bot/Activity processes,
and pauses new attempts for 60 seconds following a provider failure. The 240-second
cache age is configurable (60..600); it is not assumed token TTL. Login and
validation run in a worker thread, leaving the async bot event loop responsive.

Managed requests use the exported test character for the authentication header
and generate a fresh callback ID, instead of reusing a legacy account ID in
WWM_UID. Explicit uid/token overrides remain explicit. Default payload callback
replacement operates on a copy and does not mutate a caller's dictionary.

Each host has its own context/cache/locks. PC and Pi can use the same source code
and different environment files. If using the same test-game account on both,
avoid simultaneous manual fetches: native handover replaces that account's
existing game session. There is no distributed lock between computers.

When configured, the verified SDK rejection code 40046 triggers one headless
Steam/SDK renewal and one validation attempt before replacing the context.
There is no character creation, gameplay RPC or RAM read. Failed renewal applies
a cooldown and preserves the old context. Steam credentials can still be
revoked; this is not a guarantee of permanent unattended access.

## Updates and checks

Later Git pulls update the runtime while preserving `.env`, private context and
caches. Re-run setup when dependency pins change. A game protocol/key/routing
change may require a new verified context; no game files are changed by this tool.
The original research export helper is a Windows development tool, not a Pi
dependency. Windows DPAPI token files are not needed or copied to the Pi.

```sh
.venv/bin/python -m unittest discover -s maintenance/checks/tests -p test_portable_wwm_auth.py -v
```

Diagnostics: `.wwm-auth/last-result.json` is redacted; `token-cache.json` contains
the actual private API token. A hard-killed provider may leave `provider.lock`;
check that no provider is running before removing that stale lock. Normal exits
remove it. Do not delete credentials as a troubleshooting step.
# Diagnosing a failed refresh

The provider waits 60 seconds after a failed login to avoid a retry loop.
The cooldown is not the original failure. Errors now include a safe reason
and remaining wait; private `failure.json` also records that reason without
credentials. After the wait, run `python -m wwm_auth --fetch` from the bot
folder to make one bounded test-account attempt without starting Discord.
Do not delete the cooldown file repeatedly or replace the credential with
your main account. A successful old `last-result.json` describes only that
earlier attempt, not the current provider state.
