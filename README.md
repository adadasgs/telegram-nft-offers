# Telegram NFT Offers

Telegram Business bot for sending custom NFT gift-offer messages in seller chats.

## Command

Send this one line in the seller's private chat from the connected Business account:

```
https://t.me/nft/SnoopCigar-84846 2300 ru
```

Supported language codes: `ru`, `en`, `zh`. The bot accepts any Telegram collectible-gift link in the form `https://t.me/nft/<gift-slug>` and a positive Stars amount.

## Features

- Sends the offer from the connected Telegram Business account.
- Adds **Accept** and **Decline** inline buttons.
- On acceptance, shows a link to `@Autooffersgift` and a **Confirm transfer** button.
- Stores offer status in SQLite.
- Includes a health endpoint for Render.

## Important limitation

These are custom offer messages, **not Telegram's official protected Gift Offers**. Telegram's official `payments.sendStarGiftOffer` method is available only to user accounts, not bots. This bot does not reserve, hold, or automatically pay Stars and cannot independently verify an NFT transfer. A “transfer confirmed” status only records that a participant pressed the button; verify ownership and payment separately.

## Deploy to Render

The included `render.yaml` creates a Python web service with a persistent disk. The Render `starter` instance and persistent disk are paid features.

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Enable Business Mode for the bot in BotFather, if it is not already enabled.
3. In Render, create a new service from this repository (or use the Blueprint from `render.yaml`).
4. Use these commands if configuring manually:
   - Build: `pip install -r requirements.txt`
   - Start: `python bot.py`
5. Add the environment variable `BOT_TOKEN` with your BotFather token. Never commit the token to GitHub or send it in chat.
6. Deploy and wait for the service health check to pass.
7. In Telegram, open **Settings → Telegram Business → Chatbots**, connect the bot, and allow it to reply in the relevant private chats.
8. In a seller's private chat, send the command above from the connected Business account.

## Render environment

The Blueprint configures:
- `BOT_TOKEN` — add this secret manually in Render.
- `DB_PATH=/var/data/offers.sqlite3` — SQLite database on the persistent disk.
- `TRANSFER_URL=https://t.me/Autooffersgift` — destination link shown after acceptance.

Do not paste the bot token into source files.
