# -- coding: utf-8 --
import os
import json
import time
import asyncio
import threading
from pathlib import Path
from decimal import Decimal, getcontext
from dataclasses import dataclass
from datetime import datetime, timezone

from web3 import Web3
from web3.exceptions import TransactionNotFound, TimeExhausted

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

getcontext().prec = 50

# ========= ENV SAFE LOAD =========
def _get_env(key: str, default: str | None = None) -> str:
    v = os.getenv(key, default if default is not None else "")
    if v is None:
        v = ""
    return v.strip()

def _get_required_env(key: str) -> str:
    v = _get_env(key, "")
    if not v:
        raise RuntimeError(f"ENV missing: {key}")
    return v

def _get_checksum_addr(key: str) -> str:
    raw = _get_required_env(key)
    try:
        return Web3.to_checksum_address(raw)
    except Exception as e:
        raise RuntimeError(f"{key} invalid address: {raw} ({e})")

BOT_TOKEN       = _get_required_env("BOT_TOKEN")
ADMIN_USER_ID   = int(_get_env("ADMIN_USER_ID", "0"))

BSC_RPC         = _get_env("BSC_RPC", "https://bsc-dataseed.binance.org")

TOKEN_ADDRESS   = _get_checksum_addr("TOKEN_ADDRESS")
TOKEN_DECIMALS  = int(_get_env("TOKEN_DECIMALS", "18"))

INCASSO_ADDRESS = _get_checksum_addr("INCASSO_ADDRESS")
TREASURY_ADDRESS= _get_checksum_addr("TREASURY_ADDRESS")
TREASURY_PRIVKEY= _get_required_env("TREASURY_PRIVKEY")

RATE            = Decimal(_get_env("RATE", "1900000"))
BONUS_BPS       = int(_get_env("BONUS_BPS", "5000"))
MIN_BNB         = Decimal(_get_env("MIN_BNB", "0.01"))
MAX_BNB         = Decimal(_get_env("MAX_BNB", "1"))
AUTO_PAYOUT     = _get_env("AUTO_PAYOUT", "1") == "1"
DAILY_CAP_MYST  = Decimal(_get_env("DAILY_CAP_MYST", "5000000"))
MAX_PER_TX_MYST = Decimal(_get_env("MAX_PER_TX_MYST", "2000000"))

# Railway: se PUBLIC_URL vuota, usa dominio pubblico automatico
PUBLIC_URL = _get_env("PUBLIC_URL")
if not PUBLIC_URL:
    rail = _get_env("RAILWAY_PUBLIC_DOMAIN")  # es: web-production-xxxx.up.railway.app
    if rail:
        PUBLIC_URL = f"https://{rail}"

PORT = int(_get_env("PORT", "8080"))

# Log diagnostico minimale (senza segreti)
print("ENV CHECK →",
      {"PUBLIC_URL": PUBLIC_URL,
       "TOKEN_ADDRESS": TOKEN_ADDRESS,
       "INCASSO_ADDRESS": INCASSO_ADDRESS,
       "TREASURY_ADDRESS": TREASURY_ADDRESS})

# ========= Web3 =========
w3 = Web3(Web3.HTTPProvider(BSC_RPC, request_kwargs={"timeout": 15}))

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"transfer", "outputs":[{"name":"","type":"bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"_owner","type":"address"}],
     "name":"balanceOf", "outputs":[{"name":"balance","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

token = w3.eth.contract(address=TOKEN_ADDRESS, abi=ERC20_ABI)
TOKEN_SYMBOL = None

# ========= Idempotenza =========
PROCESSED_FILE = Path("processed_tx.json")
_processed: dict[str, dict] = {}
_processed_lock = asyncio.Lock()

def _load_processed():
    global _processed
    if PROCESSED_FILE.exists():
        try:
            _processed = json.loads(PROCESSED_FILE.read_text())
        except Exception:
            _processed = {}
    else:
        _processed = {}

def _save_processed():
    try:
        PROCESSED_FILE.write_text(json.dumps(_processed, indent=2))
    except Exception:
        pass

async def mark_processed(txhash: str, dest_wallet: str, amount_myst_units: int):
    async with _processed_lock:
        _processed[txhash.lower()] = {
            "wallet": dest_wallet,
            "amount_myst_units": str(amount_myst_units),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _save_processed()

async def is_processed(txhash: str) -> bool:
    async with _processed_lock:
        return txhash.lower() in _processed

# ========= Utils =========
@dataclass
class Quote:
    bnb_amount: Decimal
    myst_base: Decimal
    myst_bonus: Decimal
    myst_total: Decimal

def quote_for_bnb(bnb_amount: Decimal) -> Quote:
    base = bnb_amount * RATE
    bonus = (base * Decimal(BONUS_BPS)) / Decimal(10000)
    total = base + bonus
    return Quote(bnb_amount, base, bonus, total)

def myst_to_units(amount_myst: Decimal) -> int:
    return int((amount_myst * (Decimal(10) ** TOKEN_DECIMALS)).to_integral_value())

# ========= Bot Commands =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🚀 MYST Presale — Phase 1\n\n"
        f"• Wallet incasso: {INCASSO_ADDRESS}\n"
        f"• 1 BNB = {RATE:,} MYST (+{BONUS_BPS/100:.0f}% bonus)\n"
        f"• Min: {MIN_BNB} BNB — Max: {MAX_BNB} BNB\n\n"
        "1) Invia BNB al wallet sopra\n"
        "2) /submit <txhash> <tuoWalletBSC>\n\n"
        "Comandi: /wallet • /price • /status <txhash>"
    )
    await update.message.reply_text(txt)

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💼 Wallet incasso (BNB):\n{INCASSO_ADDRESS}", quote=True)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = quote_for_bnb(Decimal("1"))
    await update.message.reply_text(
        f"💰 1 BNB = {int(q.myst_base):,} MYST (+{int(q.myst_bonus):,}) → {int(q.myst_total):,} MYST"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /status <txhash>")
        return
    txhash = context.args[0].strip()
    await verify_tx_and_show(update, txhash, None, preview_only=True)

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /submit <txhash> <tuoWalletBSC>")
        return
    txhash = context.args[0].strip()
    dest_wallet = context.args[1].strip()
    try:
        dest_wallet = Web3.to_checksum_address(dest_wallet)
    except Exception:
        await update.message.reply_text("❌ Wallet BSC non valido.")
        return
    await verify_tx_and_show(update, txhash, dest_wallet, preview_only=False)

async def verify_tx_and_show(update: Update, txhash: str, payout_wallet: str | None, preview_only: bool):
    msg = await update.message.reply_text("🔎 Verifico la transazione su BSC…")
    txhash = txhash.strip()

    if not (txhash.startswith("0x") and len(txhash) == 66):
        await msg.edit_text("❌ Tx hash non valido (formato errato).")
        return

    if not preview_only and await is_processed(txhash):
        await msg.edit_text("✅ TX già registrata e pagata in precedenza.")
        return

    try:
        tx = w3.eth.get_transaction(txhash)
    except TransactionNotFound:
        await msg.edit_text("❌ TX non trovata su BSC. Riprova tra 30–60 secondi.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Errore get_transaction: {e}")
        return

    try:
        receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60, poll_latency=2)
    except TimeExhausted:
        await msg.edit_text("⏱️ TX non ancora confermata (timeout 60s). Riprova tra poco.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Errore receipt: {e}")
        return

    if receipt.status != 1:
        await msg.edit_text("❌ La TX non è success (status != 1).")
        return

    to_addr = tx.get("to")
    if not to_addr:
        await msg.edit_text("❌ La TX non ha campo 'to' (contratto/chiamata interna).")
        return

    try:
        to_addr = Web3.to_checksum_address(to_addr)
    except Exception:
        await msg.edit_text("❌ Indirizzo di destinazione non valido nella TX.")
        return

    if to_addr != INCASSO_ADDRESS:
        await msg.edit_text(
            "❌ La TX non è diretta al wallet d’incasso ufficiale.\n"
            f"Atteso: {INCASSO_ADDRESS}\nTrovato: {to_addr}"
        )
        return

    bnb_amount = Decimal(Web3.from_wei(tx["value"], "ether"))
    if bnb_amount < MIN_BNB or bnb_amount > MAX_BNB:
        await msg.edit_text(f"❌ Importo fuori limiti. Min {MIN_BNB} – Max {MAX_BNB} BNB.")
        return

    q = quote_for_bnb(bnb_amount)
    base_text = (
        "✅ Transazione verificata!\n"
        f"• From: {tx['from']}\n"
        f"• To: {INCASSO_ADDRESS}\n"
        f"• Amount: {bnb_amount} BNB\n\n"
        "💎 MYST:\n"
        f"• Base: {int(q.myst_base):,}\n"
        f"• Bonus (+{BONUS_BPS/100:.0f}%): {int(q.myst_bonus):,}\n"
        f"• Totale: {int(q.myst_total):,}\n"
    )

    if preview_only or not AUTO_PAYOUT:
        await msg.edit_text(base_text + "🧾 Modalità anteprima. Nessun payout eseguito.")
        return

    if q.myst_total > MAX_PER_TX_MYST:
        await msg.edit_text(base_text + f"⚠️ Limite payout: {int(MAX_PER_TX_MYST):,} MYST. Contatta supporto.")
        return

    if not payout_wallet:
        await msg.edit_text(base_text + "❗ Usa: /submit <txhash> <walletBSC>")
        return

    try:
        payout_tx = send_erc20(payout_wallet, q.myst_total)
        await mark_processed(txhash, payout_wallet, myst_to_units(q.myst_total))
        await msg.edit_text(base_text + f"📨 Payout a {payout_wallet}\nTx: https://bscscan.com/tx/{payout_tx}")
    except Exception as e:
        await msg.edit_text(base_text + f"❌ Errore invio payout: {e}")

def send_erc20(to_address: str, amount_myst: Decimal) -> str:
    bal = token.functions.balanceOf(TREASURY_ADDRESS).call()
    need = myst_to_units(amount_myst)
    if bal < need:
        raise RuntimeError("Tesoreria senza MYST sufficienti.")

    nonce = w3.eth.get_transaction_count(TREASURY_ADDRESS)
    gas_price = w3.eth.gas_price

    tx = token.functions.transfer(
        Web3.to_checksum_address(to_address), need
    ).build_transaction({
        "from": TREASURY_ADDRESS,
        "nonce": nonce,
        "gasPrice": gas_price,
    })

    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 120000

    signed = w3.eth.account.sign_transaction(tx, private_key=TREASURY_PRIVKEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return Web3.to_hex(tx_hash)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando non riconosciuto. Prova /start.")

# ========= Main =========
async def main():
    global TOKEN_SYMBOL
    try:
        TOKEN_SYMBOL = token.functions.symbol().call()
    except Exception:
        TOKEN_SYMBOL = "MYST"

    _load_processed()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL non disponibile. Imposta PUBLIC_URL o usa RAILWAY_PUBLIC_DOMAIN.")

    PATH = f"bot{BOT_TOKEN}"
    WEBHOOK_URL = f"{PUBLIC_URL.rstrip('/')}/{PATH}"
    print(f"🌐 Imposto webhook: {WEBHOOK_URL}")

    await app.bot.set_webhook(WEBHOOK_URL)
    await app.run_webhook(listen="0.0.0.0", port=PORT, url_path=PATH)

def _keep_alive():
    while True:
        time.sleep(600)

if _name_ == "_main_":
    threading.Thread(target=_keep_alive, daemon=True).start()
    import nest_asyncio
    nest_asyncio.apply()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot stopped.")






