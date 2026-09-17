# Handing this off

This directory is a small kit you give to someone who already runs Hermes, so
they can point their install at a custom repo/ref, update through the normal
update path, and put everything back afterwards.

Three files matter to them: `hermes-update-rehearsal.sh` (macOS/Linux),
`hermes-update-rehearsal.ps1` (Windows), and `PLAN.md` if they want the detail.
`smoke-test.*` is for you, not them.

---

## 1. What to send

Zip the directory and send it however you like (Discord, email, a gist):

```bash
cd ~/Documents && zip -r hermes-update-rehearsal.zip hermes-update-rehearsal
```

```powershell
Compress-Archive -Path "$HOME\Documents\hermes-update-rehearsal" `
                 -DestinationPath "$HOME\Documents\hermes-update-rehearsal.zip" -Force
```

Fill these in before you send, because the recipient cannot guess them:

| placeholder | meaning | example |
|---|---|---|
| `SOURCE` | repo their update should pull from | `https://github.com/<you>/hermes-agent.git` |
| `REF` | branch or tag in that repo | `<your-branch>` |
| `WHY` | one line on what you changed | "the desktop updater rewrite" |

---

## 1b. Or: run straight from a gist

Fewer moving parts than a zip — the recipient needs nothing on disk. Put the two
scripts in a gist, then use each file's **per-file raw URL**
(`.../raw/<filename>`), never the gist-wide `/raw/` URL: that concatenates
every file in the gist into one blob, so you would be piping two scripts into
one shell.

Pin the revision too, so the recipient runs the exact file you tested instead of
whatever the gist says later:

**macOS / Linux**

```bash
curl -fsSL '<RAW_URL>' | bash -s -- pre --source '<SOURCE>' --ref '<REF>'
```

**Windows**

```powershell
& ([scriptblock]::Create((irm '<RAW_URL>'))) pre -Source '<SOURCE>' -Ref '<REF>'
```

`post` is the same command with `pre` replaced by `post`.

Two things worth knowing about this form:

- The PowerShell one is not blocked by `-ExecutionPolicy`. That policy governs
  files, and this never writes one.
- Piped in, a script cannot see its own path. The "run this next" line it prints
  therefore says `hermes-update-rehearsal.sh post --backup-root …` rather than a
  path, plus a reminder to re-run the same one-liner. That is expected, not a
  failure.

---

## 2. The message to paste

> I'd like you to test a Hermes change on your install. It's reversible, but
> please read the first paragraph before you start.
>
> **What this does:** it backs up your entire Hermes install, then points your
> updater at my fork's `<REF>` instead of the official repo, so `hermes update`
> pulls my code. Nothing is updated until *you* run `hermes update`. When you're
> done, `post` wipes and puts your backup back.
>
> **What it touches:** your whole Hermes install (`HERMES_HOME`), the desktop
> app's data, the `hermes` commands on your PATH, and one line in your global
> git config. `post` undoes all of it. If you stop part-way, run `post` anyway —
> don't just walk away from it.
>
> **Before you start:** you need `git`, `tar` and `python3` on your PATH, about
> as much free disk as your install takes, and ~30 seconds of network. Close the
> desktop app and any running Hermes sessions first — a running process holds
> files the updater needs.
>
> If you're on Windows and `python3` is the Microsoft Store stub, install a real
> Python first; the script will tell you if it can't find one.
>
> **1. Back up and point it at my code**
>
> ```bash
> ./hermes-update-rehearsal.sh pre --source SOURCE --ref REF
> ```
>
> (Windows: `powershell -ExecutionPolicy Bypass -File hermes-update-rehearsal.ps1 pre -Source SOURCE -Ref REF`)
>
> It prints where your backup went and the exact next commands. Read that output
> — it's the source of truth rather than this message.
>
> **Check the block it prints at the top before it does anything.** It looks
> like this, and it tells you which install it's about to touch:
>
> ```
> === your install ===
> HERMES_HOME   ...
> install       ...
> desktop data  ...
> backup root   ...
> ```
>
> It follows your `HERMES_HOME`. If you run Hermes with a profile (or a
> non-default home) and that block shows the wrong place, stop, `export` the
> right `HERMES_HOME`, and run `pre` again. If it's showing the wrong home,
> `post` would restore the wrong home too.
>
> **2. Update and use it**
>
> ```bash
> cd "$HERMES_HOME/hermes-agent" && hermes update
> ```
>
> (If your install isn't in the default place, use your path. `pre` prints it.)
> Then use Hermes normally for a bit — that's the part I actually need tested.
> If you use the desktop app, update it from Settings → About → Update now
> instead, so I get the app's own updater covered too.
>
> **3. Put it back**
>
> ```bash
> ./hermes-update-rehearsal.sh post
> ```
>
> Close the app first. It removes the redirect, wipes, restores your backup, and
> reports how exactly it matched. Keep the backup folder until that looks right.
>
> **Send me back:** the output of all three steps, and anything that looked
> wrong. If `post` says entries didn't match, send me that too — it's the most
> useful thing you could find.

---

## 3. Before you send it

- [ ] `bash smoke-test.sh` and `smoke-test.ps1` are both green. The kit's
      restore path runs on someone else's machine; this is the only test it has.
- [ ] `SOURCE`/`REF` actually contain the code you want tested — the script
      defaults to the fork's `main`, so if you're testing a branch you must pass
      `--ref`.
- [ ] You've said out loud that the recipient's install gets rewritten, even
      though it's reversible.

## 4. After they run it

- [ ] Confirm they ran `post` — a half-finished run leaves a global git-config
      redirect behind. If they didn't, the manual undo is:
      `git config --global --unset-all url.<SOURCE>.insteadOf` plus
      `rm "$HERMES_HOME/.skip_upstream_prompt"`.
- [ ] Their "nothing was lost" claim is a self-report. If it matters, have them
      run `hermes doctor` and open the desktop app once before deleting the
      backup.
