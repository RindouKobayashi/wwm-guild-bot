# Live-chat emotes

Game emotes are ordinary image attachments. No Discord application emoji upload,
server emoji slots or Developer Portal setup is needed.

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
