# Inline live-chat emoji uploads

These files are application emojis owned by the bot, not guild/server emojis.
Animated GIFs stay animated; the heart is a still PNG. Every upload is 128×128
and at most 256 KiB. The contact sheet shows the exact files and versioned names.

After reviewing and committing this pack, pull on the Pi and start normally.
Startup verifies the authenticated application against the environment ID below,
uploads missing files and saves local receipts under `data/inline-emojis.json`.
Test and production receipts remain separate. A bounded startup sync can resume
on the next start; successfully uploaded icons become usable immediately at login.
Missing uploads show readable names rather than breaking chat. No emoji is deleted.

Received chat markers contain the emote ID directly: `[#220]` means inline
emote 220, even without `ext.emotion`. The outgoing game input uses a separate
position-map format; it is not used to decode received messages. The relay uses
only matching application receipts. Unknown IDs stay text; missing uploads use
readable names. Emoji-only messages are not translated or duplicated.
Standalone stickers are unchanged.

For a read-only preview run:
`python maintenance/assets/upload_inline_emojis.py --environment test`
Add `--upload` for an interactive confirmation, or upload these files manually
in the bot application's Developer Portal and run `--import-existing`.
Uncertain upload outcomes are reconciled before another create is attempted.

Approved applications:
- test: `1289424877916393547`
- production: `1488802246081773669`

Reference: https://docs.discord.com/developers/resources/emoji
