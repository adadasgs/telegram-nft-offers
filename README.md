# Telegram NFT Offers

Telegram Business bot for sending formatted NFT gift offer messages.

## Command format

`https://t.me/nft/SnoopCigar-84846 2300 ru`

The last value selects the language (`ru`, `en`, or `zh`). The bot accepts Telegram collectible gift links in the form `https://t.me/nft/<gift-id>` and a positive whole-number Stars amount.

## Deploy on Render

1. Create a **Web Service** from this repository (or deploy the Blueprint in `render.yaml`).
2. Build command: `pip install -r requirements.txt`
3. Start command: `python bot.py`
4. Add `BOT_TOKEN` in Render → Environment. Get the token from @BotFather. Never put it in GitHub or this chat.
5. Deploy, then open `/health` on the Render service URL.

The included Blueprint uses Render's free plan and stores offer state in `/tmp`. That storage is temporary: pending offers may be lost when Render restarts or redeploys the service. For persistent offer history, use a paid Render persistent disk and set `DB_PATH=/var/data/offers.sqlite3` with the disk mounted at `/var/data`.

## Connect Telegram Business

In Telegram, open **Settings → Telegram Business → Chatbots**, connect this bot, and allow it to receive and send messages in the seller chats where you want to use it. Then open a seller's private chat from your Business account and send the command line there.

## Payment limitation

The **Accept** button records that the seller accepted the custom offer. It does not reserve, hold, collect, or transfer Telegram Stars and does not verify that a gift was transferred. The **Confirm transfer** button only records that a participant marked the transfer as completed. Buyer and seller must verify payment and gift transfer independently. This is not an official Telegram escrow or official Telegram Gift Offer.

## Security

Never commit the bot token. Set it only as the `BOT_TOKEN` environment variable in Render. If a token is exposed, revoke it through @BotFather and create a new one.
