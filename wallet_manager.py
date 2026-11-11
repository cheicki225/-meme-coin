"""
════════════════════════════════════════════════════════════════
WALLET MANAGER — Créer & Gérer ses Wallets (multi-wallet chiffré)
════════════════════════════════════════════════════════════════
Contrairement à wallet.py (qui charge une seule clé privée depuis
SOLANA_PRIVATE_KEY dans .env), ce module gère PLUSIEURS wallets créés
ou importés depuis le bot Telegram lui-même, avec leurs clés privées
stockées CHIFFRÉES dans le fichier de données — jamais en clair.

Sécurité :
- Les clés privées sont chiffrées avec Fernet (AES symétrique) via une
  clé de chiffrement dédiée : WALLET_ENCRYPTION_KEY dans .env
- Sans cette clé (absente ou changée), les wallets stockés deviennent
  ILLISIBLES — c'est le compromis normal du chiffrement. Sauvegarde
  cette clé aussi précieusement que les clés privées elles-mêmes.
- Une clé importée via Telegram doit être effacée du message juste après
  (fait automatiquement côté telegram_bot.py) — le texte reste cependant
  visible dans l'historique Telegram tant que Telegram lui-même ne l'a
  pas supprimé de ses propres serveurs, ce qui échappe à notre contrôle.

Si aucun wallet n'est géré ici, le bot retombe sur wallet.py (la clé
unique de SOLANA_PRIVATE_KEY) — rétrocompatibilité assurée.
"""

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from solders.keypair import Keypair

import config
import rpc_client
from manual_transfer_safety import validate_manual_transfer

log = logging.getLogger("wallet_manager")


def _get_fernet() -> Fernet:
    key = config.WALLET_ENCRYPTION_KEY
    if not key:
        raise ValueError(
            "WALLET_ENCRYPTION_KEY n'est pas configurée dans .env — "
            "impossible de chiffrer/déchiffrer les wallets gérés. "
            "Génère-en une avec generate_encryption_key() et ajoute-la à .env."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def generate_encryption_key() -> str:
    """Génère une nouvelle clé de chiffrement Fernet, à coller dans .env une seule fois."""
    return Fernet.generate_key().decode()


class WalletManager:
    def __init__(self, data_store):
        self.data_store = data_store
        self.data_store.state.setdefault("managed_wallets", {})  # {label: {encrypted_key, pubkey}}
        self.data_store.state.setdefault("active_wallet_label", None)

    # ── Création / Import ────────────────────────────────────────
    def generate_wallet(self, label: str) -> dict:
        """Génère un nouveau wallet Solana. Retourne {label, pubkey, private_key}
        — le private_key n'est retourné qu'UNE FOIS ici, jamais restocké en clair."""
        if label in self.data_store.state["managed_wallets"]:
            raise ValueError(f"Un wallet nommé '{label}' existe déjà.")

        keypair = Keypair()
        private_key_b58 = str(keypair)
        pubkey = str(keypair.pubkey())

        self._store_wallet(label, private_key_b58, pubkey)
        log.info(f"🆕 Wallet généré : {label} ({pubkey})")
        return {"label": label, "pubkey": pubkey, "private_key": private_key_b58}

    def import_wallet(self, label: str, private_key_b58: str) -> dict:
        """Importe un wallet existant depuis sa clé privée base58."""
        if label in self.data_store.state["managed_wallets"]:
            raise ValueError(f"Un wallet nommé '{label}' existe déjà.")

        try:
            keypair = Keypair.from_base58_string(private_key_b58)
        except Exception:
            raise ValueError("Clé privée invalide (format base58 attendu).")

        pubkey = str(keypair.pubkey())
        self._store_wallet(label, private_key_b58, pubkey)
        log.info(f"📥 Wallet importé : {label} ({pubkey})")
        return {"label": label, "pubkey": pubkey}

    def _store_wallet(self, label: str, private_key_b58: str, pubkey: str):
        fernet = _get_fernet()
        encrypted = fernet.encrypt(private_key_b58.encode()).decode()
        self.data_store.state["managed_wallets"][label] = {
            "encrypted_key": encrypted,
            "pubkey": pubkey,
        }
        if not self.data_store.state.get("active_wallet_label"):
            self.data_store.state["active_wallet_label"] = label
        self.data_store.save()

    # ── Lecture ────────────────────────────────────────────────────
    def list_wallets(self) -> dict:
        """Retourne {label: pubkey} — jamais les clés privées."""
        return {label: w["pubkey"] for label, w in self.data_store.state["managed_wallets"].items()}

    def get_keypair(self, label: str) -> Keypair:
        wallet = self.data_store.state["managed_wallets"].get(label)
        if not wallet:
            raise ValueError(f"Wallet '{label}' introuvable.")
        fernet = _get_fernet()
        try:
            private_key_b58 = fernet.decrypt(wallet["encrypted_key"].encode()).decode()
        except InvalidToken:
            raise ValueError(
                "Impossible de déchiffrer ce wallet — WALLET_ENCRYPTION_KEY a "
                "probablement changé depuis son stockage."
            )
        return Keypair.from_base58_string(private_key_b58)

    def get_active_label(self) -> str:
        return self.data_store.state.get("active_wallet_label")

    def get_active_keypair(self) -> Keypair:
        label = self.get_active_label()
        if not label:
            raise ValueError("Aucun wallet actif — génère ou importe un wallet, ou active-en un.")
        return self.get_keypair(label)

    def set_active_wallet(self, label: str):
        if label not in self.data_store.state["managed_wallets"]:
            raise ValueError(f"Wallet '{label}' introuvable.")
        self.data_store.state["active_wallet_label"] = label
        self.data_store.save()

    def remove_wallet(self, label: str):
        self.data_store.state["managed_wallets"].pop(label, None)
        if self.data_store.state.get("active_wallet_label") == label:
            remaining = list(self.data_store.state["managed_wallets"].keys())
            self.data_store.state["active_wallet_label"] = remaining[0] if remaining else None
        self.data_store.save()

    async def get_balance(self, label: str) -> float:
        wallet = self.data_store.state["managed_wallets"].get(label)
        if not wallet:
            return 0.0
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet["pubkey"]]}
        result = await rpc_client.rpc_post(payload, timeout=10)
        lamports = result.get("value", 0) if isinstance(result, dict) else 0
        return lamports / 1_000_000_000

    # ── Disperse SOL — transfert natif réel ─────────────────────────
    async def send_sol(self, label: str, destination: str, amount_sol: float) -> str:
        """Send a capped native SOL transfer only in explicitly enabled LIVE mode."""
        import base64
        import time
        from solders.pubkey import Pubkey
        from solders.system_program import transfer, TransferParams
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash
        import jupiter_executor
        from transaction_accounting import account_confirmed_sol_transfer

        validate_manual_transfer(
            config.TRADING_MODE,
            bool(self.data_store.state.get("emergency_stop")),
            bool(self.data_store.state.get("pending_transactions")),
            amount_sol,
            config.MAX_MANUAL_TRANSFER_SOL,
        )
        try:
            destination_key = Pubkey.from_string(destination)
        except Exception as exc:
            raise ValueError("Destination is not a valid Solana public key.") from exc

        keypair = self.get_keypair(label)
        wallet_pubkey = str(keypair.pubkey())
        if str(destination_key) == wallet_pubkey:
            raise ValueError("Source and destination wallets must be different.")
        lamports = int(amount_sol * 1_000_000_000)
        if lamports <= 0:
            raise ValueError("Transfer amount is below one lamport.")

        blockhash_payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": []}
        blockhash_result = await rpc_client.rpc_post(blockhash_payload, timeout=10)
        blockhash_str = blockhash_result.get("value", {}).get("blockhash") if isinstance(blockhash_result, dict) else None
        if not blockhash_str:
            raise ValueError("No recent blockhash; transfer cancelled.")
        await jupiter_executor._assert_blockhash_valid(blockhash_str)

        ix = transfer(TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=destination_key,
            lamports=lamports,
        ))
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(blockhash_str),
        )
        message_b64 = base64.b64encode(bytes(msg)).decode("ascii")
        fee_result = await rpc_client.rpc_post({
            "jsonrpc": "2.0", "id": 1, "method": "getFeeForMessage",
            "params": [message_b64, {"encoding": "base64", "commitment": "confirmed"}],
        }, timeout=10)
        try:
            estimated_fee = int(fee_result["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Network fee could not be verified; transfer cancelled.") from exc
        if estimated_fee < 0 or estimated_fee > config.MAX_TRANSACTION_FEE_LAMPORTS:
            raise ValueError("Estimated network fee exceeds the configured transaction limit.")

        tx = VersionedTransaction(msg, [keypair])
        signature = str(tx.signatures[0])
        self.data_store.record_pending_transaction({
            "signature": signature,
            "wallet": wallet_pubkey,
            "destination": str(destination_key),
            "amount_lamports": lamports,
            "kind": "manual_sol_transfer",
            "status": "submitted",
            "created_at": int(time.time()),
        })
        try:
            signed_b64 = base64.b64encode(bytes(tx)).decode("ascii")
            result = await rpc_client.rpc_post({
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [signed_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=15)
            if result != signature:
                raise ValueError("RPC returned a different transfer signature.")
            await jupiter_executor._confirm_transaction(signature)
            confirmed = await jupiter_executor._get_confirmed_transaction(signature)
            accounting = account_confirmed_sol_transfer(
                wallet_pubkey, str(destination_key), lamports, confirmed
            )
            self.data_store.resolve_pending_transaction(signature, accounting)
        except Exception as exc:
            if any(
                item.get("signature") == signature
                for item in self.data_store.state.get("pending_transactions", [])
            ):
                self.data_store.activate_emergency_stop(
                    f"manual transfer outcome requires reconciliation: {str(exc)[:300]}"
                )
            raise

        log.info(
            "[Manual SOL transfer] %s lamports from '%s' to %s — tx %s",
            lamports, label, str(destination_key)[:8], signature,
        )
        return signature
