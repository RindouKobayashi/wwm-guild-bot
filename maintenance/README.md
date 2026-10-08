# Bot maintenance

These files are development tools, not Discord bot runtime code.

- assets/: original artwork refresh/import tools and reviewed mapping evidence.
- checks/: the offline check launcher, runner and synthetic tests. These do not start the bot or call Discord/game APIs.
- research/: read-only game-data discovery scripts; generated reports and candidate images are in output/ (git-ignored).
- docs/: WWM change notes and dungeon artwork research.

Run Refresh WWM Ranking Assets.cmd from the bot root to refresh artwork. Run maintenance/checks/Check WWM Offline.cmd for offline checks.

Artwork refresh tools require the sibling WWM-Toolkit runtime and game-data export. Bot runtime only reads data/ranking_assets/latest.json and its selected snapshot. Older snapshots are preserved under data/ranking_assets/archive/.

The existing test/ directory is a separate legacy collection, including live API probes and LLM work. It was left intact and is never discovered by the offline runner.
