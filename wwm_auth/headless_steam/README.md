# Automatic headless TEST-account login renewal

The provider now renews a rejected WWM SDK login using SteamKit2 3.3.0 directly.
No desktop Steam, game installation, RAM access or MITM is needed at runtime.
Windows end-to-end validation succeeded: Steam ticket -> SDK login -> guarded
TEST character -> protected API lookup -> atomic context replacement. Linux ARM
execution still needs validation on your Pi.

## Automatic behavior

Keep `WWM_AUTH_MODE=saved_login` in the existing root `.env`. When both private
renewal files exist, the provider automatically handles the verified
`login_fail` code 40046. It requests one fresh Steam ticket, sends one SDK login
request, validates the new test-character token, backs up the previous context,
and installs the new context and cache while holding the provider lock.
Network errors, account mismatches, queue responses and other unsupported
continuations do not trigger renewal. A failed renewal retains the previous
context and applies the existing 60-second cooldown. There is no retry loop.
Steam can revoke the saved refresh credential; repeat setup if that happens.

## Files to commit

Commit the helper sources/lockfile/launchers, Python renewal/provider/service/CLI
changes, offline tests, setup-script change and offline template exporter.
Never commit `.wwm-auth`, compiled `bin`/`obj`, captured traffic or secrets.

## Transfer from Windows to Pi

Three PRIVATE files are needed alongside the code:

- `.wwm-auth/context.json`: guarded test WWM context.
- `.wwm-auth/steam-headless.json`: reusable test Steam login, saved by setup.
- `.wwm-auth/sdk-renewal.json`: verified SDK template/signing configuration.

All three are portable private JSON, not Windows DPAPI. Keep them private.
No password is stored. No `.env` replacement is needed.

After committing and pulling the code, run in desktop PowerShell from the bot folder:

```powershell
ssh smolsus@smolsus 'mkdir -p ~/Desktop/wwm-guild-bot/.wwm-auth && chmod 700 ~/Desktop/wwm-guild-bot/.wwm-auth'
scp .wwm-auth/context.json .wwm-auth/steam-headless.json .wwm-auth/sdk-renewal.json smolsus@smolsus:~/Desktop/wwm-guild-bot/.wwm-auth/
ssh smolsus@smolsus 'chmod 600 ~/Desktop/wwm-guild-bot/.wwm-auth/*.json'
```

On Pi install the .NET 8 SDK for its architecture, then:

```bash
cd ~/Desktop/wwm-guild-bot
bash "Setup WWM API Auth.sh"
.venv/bin/python -m wwm_auth --renew-login
```

Setup builds the helper on Pi using the pinned dependency lockfile. The explicit
renew command validates the whole chain once; it prints no ticket/token text.
Then start the bot with your existing launcher. Future SDK-login rejections are
handled automatically. Do not substitute the probe for the bot launcher.

## First sign-in or revoked Steam credential

Windows: run `Setup Headless Test Steam.cmd`, enter the TEST account credentials
in its terminal, and complete Steam Guard. Pi: run
`bash wwm_auth/headless_steam/run.sh setup`. Password input is hidden. Wrong
account identity prevents saving credentials or requesting a WWM ticket.
`probe` is available for Steam-only diagnostics and cancels the ticket on exit.
Internal `ticket` mode requires private process pipes; credentials never go to
terminal output. The parent holds the ticket until the SDK exchange completes.

## SDK template export (maintenance PC only)

`mitmdump -n -q -s maintenance/research/export_headless_sdk_config.py` exports
from the existing hash-verified TEST capture and signing evidence, with no
network calls. This was already run for the current setup. Pi needs only the
exported private file, not mitmproxy or the capture. Never export from main-account
captures. The exporter depends on the existing local research evidence.

SteamKit2 is pinned to 3.3.0 and licensed LGPL-2.1-only.
Sources: https://github.com/SteamRE/SteamKit/blob/3.3.0/Samples/000_Authentication/Program.cs
and https://github.com/SteamRE/SteamKit/blob/3.3.0/SteamKit2/SteamKit2/Steam/Handlers/SteamAuthTicket/SteamAuthTicket.cs
