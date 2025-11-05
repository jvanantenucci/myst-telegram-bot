# -- coding: utf-8 --
"""
MYST Presale Bot – auto-payout BEP-20 su BSC
Dipendenze:
  pip install python-dotenv python-telegram-bot==21.4 web3==6.11.4 requests nest_asyncio
"""

import os
import json
import asyncio
from pathlib import Path
from decimal import Decimal, getcontext
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import TransactionNotFound, TimeExhausted

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Precisione per i calcoli
getcontext().prec = 50

# ── ENV ──────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN")
ADMIN_USER_ID     = int(os.getenv("ADMIN_USER_ID", "0"))

BSC_RPC           = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org")

TOKEN_ADDRESS     = Web3.to_checksum_address(os.getenv("TOKEN_ADDRESS"))
TOKEN_DECIMALS    = int(os.getenv("TOKEN_DECIMALS", "18"))

INCASSO_ADDRESS   = Web3.to_checksum_address(os.getenv("INCASSO_ADDRESS"))

TREASURY_ADDRESS  = Web3.to_checksum_address(os.getenv("TREASURY_ADDRESS"))
TREASURY_PRIVKEY  = os.getenv("TREASURY_PRIVKEY")

RATE              = Decimal(os.getenv("RATE", "1900000"))      # MYST per 1 BNB
BONUS_BPS         = int(os.getenv("BONUS_BPS", "5000"))        # 100 bps = 1%
MIN_BNB           = Decimal(os.getenv("MIN_BNB", "0.01"))
MAX_BNB           = Decimal(os.getenv("MAX_BNB", "1"))

AUTO_PAYOUT       = os.getenv("AUTO_PAYOUT", "1") == "1"
DAILY_CAP_MYST    = Decimal(os.getenv("DAILY_CAP_MYST", "5000000"))
MAX_PER_TX_MYST   = Decimal(os.getenv("MAX_PER_TX_MYST", "2000000"))

# ── Web3 ─────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(BSC_RPC, request_kwargs={"timeout": 15}))

# ABI minimo ERC20
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"transfer", "outputs":[{"name":"","type":"bool"}], "type":"function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"_owner","type":"address"}],
     "name":"balanceOf", "outputs":[{"name":"balance","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

token = w3.eth.contract(address=TOKEN_ADDRESS, abi=ERC20_ABI)
TOKEN_SYMBOL = None

# ── Idempotenza (anti-doppio payout) ─────────────────────────────
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

# ── Utils ────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()

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

# ── Comandi base ─────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🚀 MYST Presale — Phase 1\n\n"
        f"Contribuisci in BNB e ricevi MYST.\n"
        f"• Wallet incasso: {INCASSO_ADDRESS}\n"
        f"• Prezzo: 1 BNB = {RATE:,} MYST\n"
        f"• Bonus: +{BONUS_BPS/100:.0f}%\n"
        f"• Min: {MIN_BNB} BNB  • Max: {MAX_BNB} BNB\n\n"
        "Passi:\n"
        "1) Invia BNB al wallet sopra.\n"
        "2) Usa /submit <txhash> <tuoWalletBSC> per ricevere i MYST.\n\n"
        "ℹ️ Comandi: /wallet • /price • /status <txhash>"
    )
    await update.message.reply_markdown(txt)

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💼 Wallet incasso (BNB):\n{INCASSO_ADDRESS}", quote=True)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = quote_for_bnb(Decimal("1"))
    await update.message.reply_text(
        f"💰 1 BNB = {int(q.myst_base):,} MYST (+{int(q.myst_bonus):,} bonus) → {int(q.myst_total):,} MYST"
    )

# ── Verifica e payout ────────────────────────────────────────────
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

    # formato hash
    if not (txhash.startswith("0x") and len(txhash) == 66):
        await msg.edit_text("❌ Tx hash non valido (formato errato).")
        return

    # idempotenza: blocca doppi payout
    if not preview_only and await is_processed(txhash):
        await msg.edit_text("✅ Transazione già registrata e pagata in precedenza. Nessun nuovo payout eseguito.")
        return

    # recupero tx + receipt
    try:
        tx = w3.eth.get_transaction(txhash)
    except TransactionNotFound:
        await msg.edit_text("❌ Transazione non trovata su BSC. Riprova tra 30–60 secondi.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Errore get_transaction: {e}")
        return

    try:
        receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60, poll_latency=2)
    except TimeExhausted:
        await msg.edit_text("⏱️ La transazione non è ancora confermata (timeout 60s). Riprova tra poco.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Errore receipt: {e}")
        return

    # deve essere successo e verso INCASSO_ADDRESS
    if receipt.status != 1:
        await msg.edit_text("❌ La transazione non è success (status != 1).")
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

    # importo BNB
    bnb_amount = Decimal(Web3.from_wei(tx["value"], "ether"))
    if bnb_amount < MIN_BNB or bnb_amount > MAX_BNB:
        await msg.edit_text(f"❌ Importo fuori limiti. Min {MIN_BNB} BNB – Max {MAX_BNB} BNB.")
        return

    q = quote_for_bnb(bnb_amount)

    base_text = (
        "✅ Transazione verificata!\n"
        f"• From: {tx['from']}\n"
        f"• To: {INCASSO_ADDRESS}\n"
        f"• Amount: {bnb_amount} BNB\n\n"
        "💎 MYST calcolati:\n"
        f"• Base: {int(q.myst_base):,} MYST\n"
        f"• Bonus (+{BONUS_BPS/100:.0f}%): {int(q.myst_bonus):,} MYST\n"
        f"• Totale: {int(q.myst_total):,} MYST\n"
    )

    # solo anteprima o autopayout OFF
    if preview_only or not AUTO_PAYOUT:
        await msg.edit_text(base_text + "🧾 Modalità anteprima. Nessun payout eseguito.")
        return

    # limite per singola transazione
    if q.myst_total > MAX_PER_TX_MYST:
        await msg.edit_text(
            base_text + f"⚠️ Limite per singolo payout: {int(MAX_PER_TX_MYST):,} MYST. Contatta supporto."
        )
        return

    # deve esserci wallet destinatario
    if not payout_wallet:
        await msg.edit_text(
            base_text + "❗ Nessun wallet BSC di destinazione indicato. Usa: /submit <txhash> <walletBSC>"
        )
        return

    # payout
    try:
        payout_tx = send_erc20(payout_wallet, q.myst_total)
        # marca come pagato SOLO dopo invio riuscito
        await mark_processed(txhash, payout_wallet, myst_to_units(q.myst_total))
        await msg.edit_text(
            base_text
            + f"📨 Payout inviato a {payout_wallet}.\n"
            + f"Tx: https://bscscan.com/tx/{payout_tx}"
        )
    except Exception as e:
        await msg.edit_text(base_text + f"❌ Errore invio payout: {e}")

def send_erc20(to_address: str, amount_myst: Decimal) -> str:
    """Invia MYST dal wallet tesoreria al destinatario e ritorna tx hash."""
    # check saldo tesoreria
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

    # stima gas
    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 120000

    # firma e invio (web3.py v6 -> raw_transaction)
    signed = w3.eth.account.sign_transaction(tx, private_key=TREASURY_PRIVKEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    return Web3.to_hex(tx_hash)

# ── Unknown commands ─────────────────────────────────────────────
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando non riconosciuto. Prova /start.")

# ── Main ─────────────────────────────────────────────────────────
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

    print("🤖 Bot running. Ctrl+C per fermare.")
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot stopped.")