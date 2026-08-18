# Moving the node to a different Google Drive account

Everything needed to create a Google OAuth client (the *client ID* and *client
secret*), point rclone at a new Drive account, and get the node uploading
again. Written for the state this node is actually in:

| | |
| --- | --- |
| rclone | v1.75.0 (Raspberry Pi OS 13, arm64) |
| remote name | `gdrive` |
| remote type | `drive`, `scope = drive` |
| config file | `/home/saba/.config/rclone/rclone.conf` |
| uses a custom client ID | yes — keep it that way, see [Why your own client ID](#why-your-own-client-id) |

Allow about 20 minutes. Steps 1–7 happen in a browser on any machine; steps
8 onwards happen on the Pi.

> **Never put a client secret, token, or password in a file that gets
> committed, in a chat window, or in a screenshot.** `rclone.conf` holds a live
> refresh token for the whole account and belongs only on the Pi, mode `600`.

---

## Why your own client ID

rclone ships with a shared client ID that every rclone user on earth also uses.
It still works, but that pool is heavily rate-limited, and on a node uploading
gigabytes a day you will see `403 rateLimitExceeded` and stalled transfers. A
client ID of your own gets its own quota and costs nothing.

This node already uses a private one. If you are moving to a new Google
account, create a new client under **that** account — a client ID belongs to
the Google Cloud project that made it, not to the Drive it talks to, but
keeping them together is what makes the handover clean.

---

## Part 1 — Create the OAuth client (browser)

Sign in to Google **as the new account that will own the data** before you
start. If you are signed into several accounts at once, use a private window
and sign into just the one, or you will silently create the project under the
wrong identity.

Google renamed these screens: OAuth settings moved from *APIs & Services →
OAuth consent screen* to a section called **Google Auth Platform**. Both paths
are given; the URLs are the reliable part.

### 1. Create a project

<https://console.cloud.google.com/projectcreate>

Name it something you will recognise later — `bee-node` is fine. No
organisation and no billing account is required.

### 2. Enable the Google Drive API

<https://console.cloud.google.com/apis/library/drive.googleapis.com>

Check the project selector at the top is your new project, then **Enable**.
Nothing works without this and the resulting error (`accessNotConfigured`) does
not say so plainly.

### 3. Configure the consent screen

<https://console.cloud.google.com/auth/overview>
(older projects: *APIs & Services → OAuth consent screen*)

- **User type / Audience**: **External**. "Internal" only exists on Google
  Workspace organisations, and a personal Gmail account cannot use it.
- **App name**: anything, e.g. `bee-node`. You will see it on the consent
  screen when you authorise.
- **User support email** and **Developer contact email**: the new account's
  address.

Save. You do not need a logo, a homepage, or a privacy policy for this.

### 4. Deal with the 7-day token expiry — do not skip this

A project left in **Testing** issues refresh tokens that **stop working after
7 days**. The node would upload happily for a week and then fail with
`invalid_grant` while you are not watching. For an unattended field node this
is the single most important setting on this page.

Two ways to avoid it, on the **Audience** page:

- **Publish the app** — set publishing status to **In production**. Because
  the `drive` scope is "restricted", Google shows an *unverified app* warning
  when you authorise, and you click through it via **Advanced → Go to
  bee-node (unsafe)**. Verification is only required to distribute an app to
  other people; using your own app on your own account is fine. Refresh tokens
  then do not expire on a timer.
- **Or stay in Testing and accept re-authorising every 7 days.** Only sensible
  for a bench test you are watching.

Publish it. A node that dies quietly after a week in a field is the failure
this project cannot afford.

### 5. Add the account as a test user (only if you stayed in Testing)

*Audience → Test users → Add users* → the new account's email. Skip if you
published.

### 6. Choose the scope

You will not be asked to pick a scope in the console — rclone requests it — but
decide now, because it determines what the credentials can reach:

| scope | what it can touch | use when |
| --- | --- | --- |
| `drive` | the **entire** Drive of that account | current setting; needed because the node reads `IoT/config.ini`, a file a person created |
| `drive.file` | only files the app itself created | safer, but the node could not read a config file you uploaded by hand |

This node uses `drive`. Be aware that whoever holds the Pi can then read and
write that account's whole Drive. If that matters, use a Google account created
solely for the node and share nothing else into it.

### 7. Create the credentials

<https://console.cloud.google.com/auth/clients>
(older projects: *APIs & Services → Credentials*)

**Create client** → **Application type: Desktop app** → name it → **Create**.

Desktop app is correct even though this runs on a Pi: it is the flow rclone
implements, and it is what allows the headless authorisation in step 10.

Copy the **Client ID** (ends `.apps.googleusercontent.com`) and the **Client
secret** (starts `GOCSPX-`). The secret can be re-displayed later from this
same page, so there is no need to write it somewhere risky.

---

## Part 2 — Point rclone at the new account (on the Pi)

### 8. Back up the existing config first

```bash
cp -a ~/.config/rclone/rclone.conf ~/rclone.conf.before-swap
chmod 600 ~/rclone.conf.before-swap
```

That file contains a working token for the **old** account. Keep it only until
the new one is proven, then delete it — it is a live key, not a document.

### 9. Stop the node so nothing writes mid-swap

```bash
sudo systemctl stop iot-node
systemctl --user stop gdrive-mount
```

Sensor readings pause; nothing already recorded is lost.

### 10. Give rclone the new credentials

Non-interactive, which is easier to get right than the menus:

```bash
rclone config update gdrive \
    client_id     'YOUR_CLIENT_ID.apps.googleusercontent.com' \
    client_secret 'GOCSPX-YOUR_SECRET'
```

Then re-authorise, which is what actually swaps the account:

```bash
rclone config reconnect gdrive:
```

**On the Pi's own desktop**, this opens a browser: sign in as the new account,
click through the unverified-app warning (**Advanced → Go to … (unsafe)**), and
grant access.

**Over SSH with no browser**, answer `n` when it asks whether to use auto
config, then run this on a computer that *does* have a browser and rclone
installed:

```bash
rclone authorize "drive" "YOUR_CLIENT_ID.apps.googleusercontent.com" "GOCSPX-YOUR_SECRET"
```

It prints a long `{"access_token":...}` blob. Paste that back into the waiting
prompt on the Pi. The blob is a live credential — treat it like a password and
clear it from your scrollback afterwards.

If you would rather use the menus: `rclone config` → `e` (edit existing) →
`gdrive` → accept the existing answers, supply the new client ID and secret
when prompted, `n` to advanced config, then `y` to re-authorise.

### 11. Lock the file down

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

### 12. Check it is really the new account

```bash
rclone about gdrive:
rclone config userinfo gdrive:      # shows which account is authorised
```

`userinfo` is the one that matters — `about` will happily report quota for the
old account if the reconnect silently failed.

---

## Part 3 — Recreate what the node expects

A fresh Drive is empty, and the node reads its timetable from Drive. Without
this it runs on the cached local copy and your remote edits do nothing.

### 13. Rebuild the folder and upload the config

```bash
cd /home/saba/iot-node
rclone mkdir gdrive:IoT
rclone copyto var/state/config.ini gdrive:IoT/config.ini
```

`var/state/config.ini` is the node's last known-good copy, so this preserves
the timetable you are already running.

### 14. Start everything and confirm

```bash
systemctl --user start gdrive-mount
sudo systemctl start iot-node
scripts/verify-boot.sh
```

Expect all checks to pass, with `cloud reachable` showing the **new** account's
free space. Then watch one real pass:

```bash
journalctl -u iot-node -f
```

You are looking for `config.ini updated from the drive` and, at the next
upload, `uploaded … → IoT/…`.

### 15. Revoke the old account's access

<https://myaccount.google.com/permissions> — signed in as the **old** account,
remove the entry for the old OAuth app. Then delete the backup:

```bash
shred -u ~/rclone.conf.before-swap 2>/dev/null || rm -f ~/rclone.conf.before-swap
```

Until you do this, the old credentials still work for anyone holding a copy.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `invalid_grant` after about a week | project left in **Testing** | publish the app (step 4), then `rclone config reconnect gdrive:` |
| `accessNotConfigured` / `Drive API has not been used` | API not enabled | step 2, then wait a minute |
| `403 storageQuotaExceeded` | the Drive is full | free space, or use a Shared Drive / paid tier — this is a quota fact, not a credentials fault |
| `403 rateLimitExceeded`, slow or stalling uploads | using rclone's shared client ID | do Part 1 and set your own |
| `Error 400: redirect_uri_mismatch` | client created as *Web application* | recreate it as **Desktop app** (step 7) |
| Consent screen refuses the account | published app, but account not a test user while still in Testing | step 5, or publish |
| Node uploads but ignores your timetable edits | `IoT/config.ini` missing on the new Drive | step 13 |
| `rclone about` works, uploads 403 | authorised the wrong Google account | `rclone config userinfo gdrive:` to confirm, then reconnect |

## What holds the credentials

| Path | Contains | Notes |
| --- | --- | --- |
| `~/.config/rclone/rclone.conf` | client ID, client secret, refresh token | the only copy on the node; mode `600`; never commit |
| Google Cloud Console | the client ID and secret | secret re-displayable from *Clients* |
| `var/state/config.ini` | timetable only, no credentials | safe to commit or share |

The node never stores Google credentials itself — it shells out to rclone, so
`rclone.conf` is the single thing to protect and the single thing to replace.
