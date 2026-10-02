# Using anki-mcp from a phone or iPad

This lets you ask Claude on your phone to tidy the cards you typed there and add audio, while you're away from the Mac. It is free: the MCP server still runs on the Mac, and you drive a Claude Code session there through **Remote Control**. The server needs no remote transport, tunnel or auth.

```
phone (AnkiMobile) ──sync──▶ AnkiWeb ◀──sync tool── Anki on the Mac ◀── anki-mcp ◀── Claude Code session
                                                                                        ▲
phone (Claude app) ──────────── Remote Control ─────────────────────────────────────────┘
```

Phase 4 of the [roadmap](../ROADMAP.md) (a remote HTTP server) would reach the same place, but it still needs the Mac running, and it adds a public endpoint that can write to the collection. Phase 6 (headless AnkiWeb) is the real desktop-free option, and it is still unproven.

## 1. Keep the Mac awake

**System Settings:**
- Laptop: Battery → Options → turn on *Prevent automatic sleeping on power adapter when the display is off*. Keep it plugged in.
- Desktop (Mini, iMac, Studio): Energy → turn on *Prevent automatic sleeping when the display is off*.
- Lock Screen → *Turn display off*: any value works. The display can sleep; the Mac must not.

**Or from the terminal:**
```bash
sudo pmset -c sleep 0 disksleep 0   # never sleep on the charger (-c)
pmset -g                            # check that it shows "sleep 0"
```
Undo it when you're back: `sudo pmset -c sleep 1` (or your previous value, which `pmset -g` showed before the change).

**Laptop lid:** a closed lid sleeps the Mac unless an external display is attached. Leave it open. `sudo pmset -a disablesleep 1` also works with the lid closed, but it's easy to forget, and a closed laptop runs warmer. Undo it with `sudo pmset -a disablesleep 0`.

`caffeinate -dis` also works, but only while that terminal stays open.

## 2. Stop restarts

- System Settings → General → Software Update → Automatic Updates: turn off *Install macOS updates* and *Install Security Responses* until you're back.
- Desktop Macs only: `sudo pmset autorestart 1` (or Energy → *Start up automatically after a power failure*).
- **FileVault** stops the Mac at the password screen after any restart, so Anki and Claude won't come back by themselves. Accept it: after a restart you have no Anki on the Mac until you're home. Your phone keeps working normally.
- Add Anki to System Settings → General → Login Items anyway, which helps after short interruptions.

## 3. Keep Anki ready

- Anki open, with AnkiConnect enabled, and **logged in to AnkiWeb** (press Sync once by hand to check).
- No open dialogs. AnkiConnect waits while a modal is open, and the tools then time out.
- **Don't change note types or fields while you're away** (for example, adding an Audio field). That forces a full sync, which needs someone at the Mac to choose which side to keep. The `sync` tool refuses full syncs and says so. It doesn't hang.

## 4. Start the Claude session

Run it inside `tmux`, so closing the terminal window doesn't end it:
```bash
brew install tmux                      # once
tmux new -s anki
cd ~/Projects/anki-mcp && claude       # then type /remote-control
```
Detach with `Ctrl-b d`. Reattach later with `tmux attach -t anki`. Open the session from the Claude app on your phone or iPad.

## 5. Daily flow

1. Add words in AnkiMobile or AnkiDroid, then **sync the phone**.
2. In the Claude app: *"tidy the Spanish cards I added on my phone and add audio"*.
3. Claude calls `sync` (to pull your phone's cards), fixes them, voices them, and calls `sync` again (to push).
4. Wait about a minute (audio uploads in the background), then **sync the phone** again. The audio is there.

## 6. Test it before you leave

Leave everything running overnight. The next morning, add a card on your phone and run the daily flow from the phone.

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| "Cannot reach AnkiConnect" | Anki closed or crashed, or the Mac restarted | Wait until you're home. The phone works as normal. |
| "did not answer … within 120s" | Anki is showing a dialog | Wait until you're home, or use Screen Sharing if you have it. |
| "AnkiWeb needs a full sync" | Note-type or field change on some device | Resolve it at the Mac: click Sync and choose which side to keep. |
| Phone cards not found | The phone didn't sync, or the Mac sync failed | Sync the phone and ask again. |
| Audio missing on phone | Media still uploading | Wait a minute and sync the phone again. |

## When you're back

`sudo pmset -c sleep 1` (and `disablesleep 0` if you set it), turn automatic updates back on, and end the tmux session.
