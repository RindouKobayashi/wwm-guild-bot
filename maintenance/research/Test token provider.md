# Windows test-account token provider

## Without Steam (verified saved login)

Run **Get Test WWM API Token Without Steam.cmd**. It uses only the hash-verified
saved TEST-account game-login credential. It does not call Steam, request a
Steam ticket, authenticate with the SDK, or read game memory. Current local
Steam account selection is irrelevant to this mode; there is no main-account
fallback. It may replace a lingering test-account session through native handover.

Two independent runs passed protected own-character API validation after fixing
initial-snapshot parsing: initial properties can be nested inside
PropItem.dict/PropDict.item, rather than flat update values. The reader now follows
that schema with depth, item and byte limits. Both available token candidates
are retained instead of overwriting one with the other. Twenty-nine offline tests
pass, including nested values, name-only rejection and recursion bounds.

The normal launcher path was also verified end to end. Each invocation has its
own receipt, uses the shared provider lock, makes one attempt and rejects stale
results. Saved-login results are separate from the original Steam-provider result.
The saved login's validity period and renewal are still unknown: an accepted
reconnect does not prove permanent reuse. Retrieving a valid token need not mint
a different token on every launch. No automatic fallback to Steam is performed.

## Fresh Steam authentication (original path)

Run **Get Test WWM API Token.cmd** in this folder. Keep Steam signed into the
verified test account and close WWM first. No game launch, RAM capture, Discord
bot, mitmweb window or new traffic recording is needed. The installed mitmdump
runtime supplies Python dependencies with its proxy server disabled.

Each explicit launch makes one bounded attempt: check the Steam account,
request a fresh ticket, authenticate with the official SDK, establish the game
connection, select/reconnect only the verified test character, receive its API
token, validate it through the protected player API, disconnect, and cancel
the Steam ticket. It can replace a lingering TEST-account session through the
native handover action. A different Steam account is never used as fallback.

The latest token is saved encrypted for the current Windows user in
`output/test-steam-20261018/api-token.user-dpapi.json`.
It is not printed or installed into `.env` or bot settings. Results and private
logs are under `output/test-steam-20261018/provider-runs/`.

Two independent standalone runs succeeded with WWM closed, including the
existing-session handover path. The second token differed from the first. A
separate protected API request after helper exit also passed for the exact
test character. Twenty-seven offline tests cover codecs, routing restrictions,
error callbacks, binary reconnect data, initial properties and validation.

This research tool requires the existing private test captures and verified
native metadata in this workspace. Keep them in place and excluded from Git:
the original `.mitm` files contain credentials. The API token is DPAPI-encrypted.
The fresh Steam launcher now also exports the validated test-game login as private
plaintext `.wwm-auth/context.json`, backing up the old context in `.wwm-auth/backups/`.
Never commit these files. Game/SDK updates can require refreshing inputs.

Fresh authentication needs Windows Steam and private research inputs. The separate
`wwm_auth` runtime can use the exported login on the Pi without Steam or research
inputs; see `wwm_auth/README.md`. Verify the new context with
`python -m wwm_auth --fetch` before securely transferring it. The Windows DPAPI
token file cannot be used on Linux. No automatic credential transfer or Steam
renewal is installed; saved-login lifetime remains unknown.

The captured token timestamp was approximately ten minutes ahead of the
request, but expiry at that boundary has not been measured. Do not assume a
stored token is permanent. Each launcher run attempts to obtain a valid token;
there is no background refresh loop or automatic retry.
