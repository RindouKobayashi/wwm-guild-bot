# Discord path icons

Open contact-sheet.html. Each displayed PNG in icons/ is one application emoji, shared by its listed kungfu.

Run maintenance/assets/upload_martial_emojis.py --environment test --pack "<upload-pack-folder>" for a read-only preview. Add --upload for interactive confirmation. Production requires its application ID. No server emoji upload is needed.

Manual fallback: Developer Portal → your application → Emojis. Upload the listed PNG using its exact manifest name, then run the helper with --import-existing to verify remote artwork and save assigned IDs. Never use the test application registry for production.
