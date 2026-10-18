# Uncommitted authentication change: review/commit list

Branch inspected: `dev`. No staging, commit, push, branch switch or Pi deployment
was performed. This list covers the portable provider, not all pre-existing work
in the repository. Review the complete diff of shared modified files before
committing them; utility/wwm.py already contained earlier authentication research
changes, and other Activity changes are unrelated to this package.

## New runtime and documentation files to include

```text
wwm_auth/__init__.py
wwm_auth/__main__.py
wwm_auth/context.py
wwm_auth/protocol.py
wwm_auth/session.py
wwm_auth/provider.py
wwm_auth/service.py
wwm_auth/example.env
wwm_auth/README.md
wwm_auth/REVIEW.md
requirements-auth.txt
Setup WWM API Auth.sh
Check WWM API Auth.cmd
maintenance/checks/tests/test_portable_wwm_auth.py
```

## Existing tracked files whose relevant changes must be included

```text
.gitignore
requirements.txt
settings.py
utility/wwm.py
Setup Raspberry Pi.sh
Raspberry Pi Setup.md
```

The whole `wwm_auth` package is required. Committing only the utility hook or only
the setup script will not produce a working checkout. No file in
maintenance/research is imported by the runtime; those experiments and launchers
are optional development material, not required on the Pi. In particular the
large collection of untracked research scripts does not have to be committed for
this feature to work. Keep unrelated Activity edits out of this change unless
you independently intend to include them.

## Never commit; transfer/configure separately

```text
.env, .env-backup and Activity environment profiles
.wwm-auth/context.json
.wwm-auth/token-cache.json and all other .wwm-auth files
maintenance/research/output/ (captures, DPAPI files, monitoring results, wheels)
.venv/, logs/, databases
```

`.wwm-auth/context.json` must be transferred securely to the Pi once. It is
plaintext private test-login material, not an example configuration. The example
environment file contains only setting names/defaults and belongs in Git.

## Validation and remaining limitations

- Standalone local Python login and exact test-character protected lookup passed.
- Offline tests cover account/device guards, nested snapshots, RPC selection,
  binary relay data, cache isolation/cooldown, explicit token bypass and imports
  from a copy with no research code.
- Linux ARM64 Python 3.11 dependency wheels are available; actual Pi execution
  remains to be validated after pull/private transfer.
- No bot or Discord session was launched, and no .env was edited.
- API-token rejection/replacement was observed; saved-login expiry and renewal
  remain unknown. The package does not promise indefinite unattended access.
