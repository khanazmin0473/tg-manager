# Telegram Manager (Bot + Mini App)

A modern Multi-Account Telegram Manager powered by FastAPI, Python Telegram Bot, and Telethon.

## Features
- Unified event loop for flawless Telegram Bot + Mini App coordination
- Add multiple Telegram accounts via Phone number (OTP + 2FA) or Contact sharing
- View recent Dialogs (Inbox) in real-time
- Send messages to any chat from the selected active account
- Protected by Telegram InitData HMAC validation & Owner ID restriction

## Environment Variables
- `BOT_TOKEN`: Your Telegram Bot Token from BotFather
- `ALLOWED_USER_ID`: Your Telegram numeric User ID
- `API_ID`: Your Telegram API ID from my.telegram.org
- `API_HASH`: Your Telegram API Hash from my.telegram.org
- `MINIAPP_URL`: Your Render Web Service URL (e.g. `https://your-service.onrender.com`)
