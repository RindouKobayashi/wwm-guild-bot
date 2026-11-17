# Live-chat emotes

Standalone game stickers remain ordinary image attachments. The small inline
faces are also bundled as bot-owned application emojis, preserving animation.
They do not consume server emoji slots. The normal startup sync uploads missing
reviewed files; no manual Developer Portal setup is needed.

## Main-computer update

Run `Update WWM Assets.cmd --only emotes --force`. Extraction runs only on the
computer with the game and Toolkit. It creates a candidate and leaves the active
release alone. Open the printed `release/emotes/contact-sheet.html` and the normal
`release/report.html`. Search by emote ID, name, collection or rendering status.

After reviewing the changes, use `Apply WWM Asset Update.cmd <printed-run-id>`.
Commit the code and `assets/wwm/current.json` plus `assets/wwm/current/`. Pull on
the Pi and use its normal launcher. Restart an already running bot to load the
new catalogue. The Pi needs no game, browser, Toolkit or conversion tools.

## What the catalogue proves

The current original chat Lua routes `MSG_EMOTION` to `pic_style`; the topic
branch (`emotion_type` 2) reads `emotion_id` from `chat_emotion_icon_lib`.
Custom/user-uploaded images (`emotion_type` 1) are a different namespace and are
never replaced with a game icon having the same number. Older API messages which
omit this field retain the existing numeric topic-emote interpretation.

The separate `chat_emotion_frame_data` index belongs to rich text. Its description
index is checked against the same rows. Its numbers are **not** fallback IDs for
standalone emotes, and inline markup behavior is unchanged. Current original Lua
checksums and required symbols are saved in the catalogue. Missing translations
remain missing, and no group icon is substituted for unavailable emote art.
Critical routing-source hashes are pinned in `emote_sources.json`. A changed
script stops extraction until its control flow is reviewed; matching symbol
names alone cannot approve a changed ID relationship.

Inline artwork comes from the client's `all_png_data` Cocos atlas list. The
extractor finds exact frame filenames in installed `.plist` files, decodes the
referenced atlas texture and reconstructs each frame using its original crop and
transparent canvas. It checks bounds, dimensions and trim offsets; rotated
frames remain unsupported until their decoding is verified. All 280 required
frames on this install are unrotated. This recovers the 55 inline emotes for the
gallery without mixing their IDs with standalone sticker IDs. Their GIF previews
use the client's default 200 ms frame
interval; custom rich-text timing is not interpreted by the relay.
The heart (261) has four identical original frames and is correctly exported as
a still; the other 54 recovered inline entries have animated previews.

## Inline replacement and automatic upload

The received-message renderer uses `[#N]` as inline emote ID N directly: for
example, `[#220]` is spit and `[#260]` is rose. `render_received()` does not
depend on `ext.emotion`, including when that field is missing or conflicting.
Unknown IDs remain literal; known faces without an upload use readable labels.
The game's outgoing input uses a separate position-map format (`[#4]` with the
ID in `ext.emotion[4]`). The legacy `render()` helper handles that format only
and is not called by the Discord relay. Received and outgoing source evidence
are pinned separately; numeric IDs never cross into the sticker catalogue.

`emotes/upload-pack/` contains 54 animated 128×128 GIFs and one PNG, a manifest,
an illustrated sheet and upload instructions. Each file is below 256 KiB.
The release approval binds the exact catalogue and upload-manifest hashes to the
test/production application IDs in `assets/discord/inline-applications.json`.
Review and commit the pack, pull on the Pi, and start normally. Both Windows
and Pi use the same bounded startup sync before gateway login; extraction and
128px conversion stay on the main computer. A separate Discord client is never
started. This is a new reviewed upload collection, separate from martial icons.

Startup checks the authenticated application's identity, reuses receipts or
verified artwork, and creates only approved missing/versioned emojis. It never
deletes old emojis. Successful IDs are saved incrementally in ignored
`data/inline-emojis.json`; pending uncertain uploads and status have separate
inline-specific files. A timeout leaves readable labels for missing uploads;
the next start reconciles before creating again. Animated reconciliation checks
every frame and timing, not only the first frame. Application capacity is checked
before any upload. No credentials appear in the pack or receipts.

Rendering uses the connected bot's application ID, not an environment guess.
It inserts `<a:name:id>` for animations and `<:name:id>` for the heart, after
translation so Discord emoji IDs are not sent to the translation provider.
Original and translated text use the markers they retain; a translator-mangled
marker is preserved rather than guessed. Emoji-only messages skip translation
and appear once. Translation runs only when text remaining outside recognized
emote markers contains Unicode letters. Each message has bounded replacements.
Standalone image cards and Discord-to-game sending remain unchanged.

For maintenance, `python maintenance/assets/upload_inline_emojis.py --environment
test` is a read-only preview. `--upload` requires typing `UPLOAD TEST`, or the
application ID in production. `--import-existing` verifies manually uploaded
artwork and cannot create emojis, including if the list changes after confirmation.
Never clear an uncertain pending upload just to retry; reconcile it first.

Discord's application-emoji format and limits:
https://docs.discord.com/developers/resources/emoji

Primary rows are bounded by the named MANIFEST page span. Actual Swiss-table
occupied counts, unique index keys, row identities and complete row consumption
are checked. On this install the manifest advertises 196 topic rows while the
primary index contains 200; the group metadata advertises 15 while its index has
13. Both counts are recorded, rather than truncating valid rows or treating
secondary indexes as additional emotes. This does not establish that every
shipped entry is unlocked or currently available to a player. New schemas or
overseas override candidates stop extraction until reviewed.

## Animation and image safety

Static images retain original alpha and proportions. UI-effect animation is
accepted only when the current generated view binds the expected original CSB,
the layout has exactly one unmodified Spine node with no additional effects or
timelines, and the parsed runtime confirms the exact `vx_loop` animation. Source
Puppet, texture, layout, Lua and installed-layer hashes accompany each conversion.

The main computer samples the original loop at 30 fps with a hidden local browser.
GIF delays distribute 30/40 ms intervals to preserve loop duration; GIF has binary
alpha, while the PNG preview retains full alpha. Images are capped at 4 MiB;
large animations are reduced proportionally before falling back to a still.
Conversions are cached under ignored `.asset-builds/` using source fingerprints.
Unsupported compositions and conversion failures stay explicit in the review.

At startup the bot validates image contents and loads the catalogue. Relaying a
message does no extraction, conversion, image decoding or remote artwork fetch.
Resolution is game asset, then approved local image, then readable text.
The destination upload limit can select a PNG preview or text. An attachment
rejection known not to have posted can fall back to text; timeouts, connection
errors and server failures are not resent because delivery is uncertain.

## Manual approvals and review

Existing files, pending submissions and persistent approval buttons remain local
under `data/emotion/`. Unknown emotes can still use the admin approval workflow;
verified game images do not prompt for manual mapping. PNG/WebP submissions are
validated against their real format, dimensions, frames and the 4 MiB limit.
Approval refreshes the in-memory fallback without restarting the bot.

The contact sheet records each previous manual file's ID, filename, checksum and
extracted status; the image itself stays out of Git. No chat content, account
identifiers, live messages, credentials or Discord upload receipts are collected.

Verification uses synthetic messages and mocked Discord sends. Actual test-server
messages and Pi deployment require separate authorization.
