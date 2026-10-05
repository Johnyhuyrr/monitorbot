# History files

These files arrived with the project and are kept for reference only. **None of them is loaded by the bot.**

| File | What it is |
|---|---|
| `bot.py.backup-2026-09-17` | The build before Ban Check and the automatic Unban monitor. It still had the *Account Support* service and a `zm_account_support` button. Panels posted by that build still show that button, and the current bot answers it politely through `LegacyButtons`. |
| `bot.py.full.patch` | Adds the automatic Unban monitor, the shared completion guard, and the `/complete` retry. |
| `bot.py.reliability.patch` | Makes the bot post the automatic-completion message *before* saving the job as completed. |

Both patches were confirmed to be fully applied in the delivered `bot.py`: `patch -R --dry-run` is clean. Their behaviour is preserved in the current code and covered by `test_auto_monitor.py` and `tests/`.
