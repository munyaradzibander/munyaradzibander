# Telegram leech + mirror bot

A small, self-hosted Telegram bot with two modes:

- **Leech:** `/leech <direct-URL>` downloads a file from a public HTTP(S) URL and sends it to Telegram.
- **Mirror:** `/mirror <direct-URL>` downloads a file and uploads it to a cloud remote configured with [rclone](https://rclone.org/). You can also reply to supported Telegram media with `/mirror` to copy it to that remote.

This bot is private by default: it only responds to numeric Telegram user IDs listed in `ALLOWED_USER_IDS`. URLs must be direct HTTP(S), redirects are rejected, private/reserved network destinations are blocked, and downloads are size-capped. Use it only for files you are authorized to access and redistribute.

## Requirements

- Python 3.10 or later
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An rclone remote for `/mirror` (for example, Google Drive). `/leech` works without rclone.
- A small Linux server for always-on polling. Free compute availability depends on provider and region.

## 1. Create your bot and get your Telegram user ID

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Run `/newbot`, follow the prompts, and save the token securely. Do not commit it to GitHub or share it.
3. Get your numeric Telegram user ID from a trusted ID lookup bot or Telegram client. Usernames are not accepted for authorization.
4. If you want group operation, add the bot to the group and obtain its numeric chat ID (often a negative number). Add that ID to `ALLOWED_CHAT_IDS` below. The bot still requires the requesting user's ID to be allowlisted.

## 2. Configure cloud storage for mirror mode

Install rclone from its official installation instructions: <https://rclone.org/install/>. Run `rclone config` on the same machine/account that will run the bot, and configure a remote, for example `gdrive`.

For Google Drive, follow the interactive `rclone config` flow and authorize the account. Then test the remote:

```bash
rclone lsd gdrive:
```

Choose a destination path, for example `gdrive:TelegramMirror`. The bot creates the destination file path; create the folder in advance if your provider requires it.

## 3. Install and configure locally

```bash
git clone https://github.com/munyaradzibander/munyaradzibander.git
cd munyaradzibander
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create a private environment file outside version control (or use `.env` locally and never commit it):

```bash
export BOT_TOKEN='123456:your-token'
export ALLOWED_USER_IDS='123456789'
export ALLOWED_CHAT_IDS=''
export RCLONE_REMOTE='gdrive:TelegramMirror'
export MAX_FILE_BYTES='47185920'
```

`MAX_FILE_BYTES` defaults to 45 MiB. Keep it within Telegram's current bot upload/download limits and your server's storage limits. Incoming Telegram files use the standard Bot API, which has its own file-download limit; the bot can only mirror files the Bot API lets it download. For private-chat testing, start it with:

```bash
python bot.py
```

In Telegram, test `/start`, then `/leech https://your-public-host.example/test.zip` and `/mirror https://your-public-host.example/test.zip`. For a Telegram upload, send a supported media file and reply to it with `/mirror`.

## 4. Free always-on hosting: Oracle Cloud Always Free VM

Oracle Cloud's Always Free compute can host a polling bot without a paid web service, subject to current account eligibility, regional capacity, and Oracle's terms. Availability and free-tier terms can change; confirm them in Oracle's official documentation before signing up. A small Ubuntu VM is sufficient for one low-traffic bot.

1. Create an Oracle Cloud account and an Always Free Ubuntu VM. Keep SSH access restricted to your own IP when possible.
2. SSH into the VM and install Python, venv, and rclone using their official instructions. Then clone the repository and install dependencies:

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv
   curl https://rclone.org/install.sh | sudo bash
   git clone https://github.com/munyaradzibander/munyaradzibander.git
   cd munyaradzibander
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure the rclone remote as the same Linux account that will run the bot. For unattended Drive access, configure credentials and refresh tokens securely; do not put `rclone.conf` in this repository.
4. Create `/etc/telegram-mirror-bot.env` with `sudo nano`:

   ```ini
   BOT_TOKEN=123456:your-token
   ALLOWED_USER_IDS=123456789
   ALLOWED_CHAT_IDS=
   RCLONE_REMOTE=gdrive:TelegramMirror
   MAX_FILE_BYTES=47185920
   ```

   Protect it: `sudo chmod 600 /etc/telegram-mirror-bot.env`. The service below runs as your Linux login user so it can access that user's rclone config. Replace `YOUR_LINUX_USER` and the repository path with your actual values.

5. Create a systemd unit at `/etc/systemd/system/telegram-mirror-bot.service`:

   ```ini
   [Unit]
   Description=Telegram mirror and leech bot
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=YOUR_LINUX_USER
   WorkingDirectory=/home/YOUR_LINUX_USER/munyaradzibander
   EnvironmentFile=/etc/telegram-mirror-bot.env
   ExecStart=/home/YOUR_LINUX_USER/munyaradzibander/.venv/bin/python /home/YOUR_LINUX_USER/munyaradzibander/bot.py
   Restart=on-failure
   RestartSec=5
   NoNewPrivileges=true
   PrivateTmp=true

   [Install]
   WantedBy=multi-user.target
   ```

6. Start and check it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-mirror-bot
   sudo systemctl status telegram-mirror-bot
   sudo journalctl -u telegram-mirror-bot -f
   ```

The bot uses Telegram long polling, so it does not need a public web port or domain. A free-tier VM is not guaranteed by every provider; if Oracle capacity is unavailable, you can run it on a machine you control, but free web-app hosts commonly sleep or do not offer free background workers, which makes polling unreliable.

## Commands and limits

- `/start` — show help.
- `/leech https://host/path/file` — download and send a direct public HTTP(S) file to the current chat.
- `/mirror https://host/path/file` — download and upload to `RCLONE_REMOTE`.
- Reply to supported Telegram media with `/mirror` — upload that media to `RCLONE_REMOTE`.

The implementation rejects redirects, credentials embedded in URLs, non-public IP destinations, non-200 responses, and files over `MAX_FILE_BYTES`. Use trusted sources: public URLs can still point to unwanted or copyrighted material. The bot does not bypass login pages, paywalls, DRM, or access controls. Telegram and storage-provider size limits apply independently. Polling is intended for a private bot; do not remove the user/chat allowlists for a public deployment without adding quotas, rate limits, and moderation.

## Troubleshooting

- **Access denied:** add the numeric user ID to `ALLOWED_USER_IDS`; for group chats, also add the chat ID to `ALLOWED_CHAT_IDS`.
- **Mirror storage is not configured:** set `RCLONE_REMOTE` and make sure rclone is installed and configured for the service user.
- **rclone errors:** run `rclone lsd gdrive:` as the Linux service user, then inspect `journalctl -u telegram-mirror-bot`.
- **Redirect message:** supply the final direct file URL. Redirects are intentionally blocked for network safety.
- **Telegram upload fails:** lower `MAX_FILE_BYTES`; Telegram imposes its own bot API limits.
- **Rotate a leaked token:** revoke/regenerate it with @BotFather, update the server environment file, and restart the service.
