"""
════════════════════════════════════════════════════════════════
DEBUG WALLET HISTORY — Diagnostic pas à pas
════════════════════════════════════════════════════════════════
Affiche chaque étape de get_created_tokens() en détail pour comprendre
précisément où ça bloque, au lieu de deviner à l'aveugle.

Usage : python debug_wallet_history.py <adresse_dev>
"""

import asyncio
import sys
import base58
import hashlib

import config
import rpc_client

CREATE_DISCRIMINATOR = hashlib.sha256(b"global:create").digest()[:8]
CREATE_V2_DISCRIMINATOR = hashlib.sha256(b"global:create_v2").digest()[:8]


async def get_signatures(address: str, limit: int = 300) -> list:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    result = await rpc_client.rpc_post(payload, timeout=15)
    return result if isinstance(result, list) else []


async def get_raw_transaction(signature: str) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    result = await rpc_client.rpc_post(payload, timeout=12)
    return result if isinstance(result, dict) else {}


async def main(dev_address: str):
    print(f"🔍 Diagnostic pour : {dev_address}\n")
    print(f"PUMP_FUN_PROGRAM_ID configuré : {config.PUMP_FUN_PROGRAM_ID}")
    print(f"Discriminant 'create'    : {list(CREATE_DISCRIMINATOR)}")
    print(f"Discriminant 'create_v2' : {list(CREATE_V2_DISCRIMINATOR)}\n")

    print("── Étape 1 : récupération des signatures ──")
    signatures = await get_signatures(dev_address, limit=300)
    print(f"Signatures récupérées : {len(signatures)}\n")

    if not signatures:
        print("❌ ARRÊT : aucune signature récupérée du tout — problème RPC/clé Helius.")
        return

    print("── Étape 2 : scan des transactions ──")
    stats = {
        "total_scanned": 0,
        "tx_vides_ou_erreur": 0,
        "avec_instruction_pumpfun": 0,
        "avec_discriminant_create": 0,
        "avec_discriminant_create_v2": 0,
        "avec_autre_discriminant_pumpfun": 0,
    }

    first_pumpfun_ix_shown = False
    first_create_shown = False
    first_create_v2_shown = False

    for i, sig_info in enumerate(signatures):
        stats["total_scanned"] += 1
        tx = await get_raw_transaction(sig_info["signature"])

        if not tx:
            stats["tx_vides_ou_erreur"] += 1
            continue

        try:
            instructions = tx["transaction"]["message"]["instructions"]
        except (KeyError, TypeError):
            stats["tx_vides_ou_erreur"] += 1
            continue

        for ix in instructions:
            program_id = ix.get("programId")
            if program_id != config.PUMP_FUN_PROGRAM_ID:
                continue

            stats["avec_instruction_pumpfun"] += 1

            raw_data = ix.get("data")
            if not raw_data:
                continue

            try:
                data_bytes = base58.b58decode(raw_data)
            except Exception:
                continue

            discriminant = list(data_bytes[:8])

            if not first_pumpfun_ix_shown:
                print(f"\n📋 Premier exemple d'instruction Pump.fun trouvée (signature {sig_info['signature'][:16]}...) :")
                print(f"   Discriminant observé : {discriminant}")
                print(f"   Nombre de comptes    : {len(ix.get('accounts', []))}")
                print(f"   Comptes              : {ix.get('accounts', [])[:8]}")
                print(f"   (structure complète de l'instruction ci-dessous)")
                print(f"   {ix}\n")
                first_pumpfun_ix_shown = True

            if data_bytes[:8] == CREATE_DISCRIMINATOR:
                stats["avec_discriminant_create"] += 1
                if not first_create_shown:
                    print(f"\n🎯 EXEMPLE CREATE (legacy) trouvé (signature {sig_info['signature']}) :")
                    print(f"   Nombre de comptes : {len(ix.get('accounts', []))}")
                    print(f"   Tous les comptes, avec leur index :")
                    for idx, acc in enumerate(ix.get('accounts', [])):
                        marker = " ← dev_address recherché !" if acc == dev_address else ""
                        print(f"     [{idx}] {acc}{marker}")
                    print()
                    first_create_shown = True
            elif data_bytes[:8] == CREATE_V2_DISCRIMINATOR:
                stats["avec_discriminant_create_v2"] += 1
                if not first_create_v2_shown:
                    print(f"\n🎯 EXEMPLE CREATE_V2 trouvé (signature {sig_info['signature']}) :")
                    print(f"   Nombre de comptes : {len(ix.get('accounts', []))}")
                    print(f"   Tous les comptes, avec leur index :")
                    for idx, acc in enumerate(ix.get('accounts', [])):
                        marker = " ← dev_address recherché !" if acc == dev_address else ""
                        print(f"     [{idx}] {acc}{marker}")
                    print()
                    first_create_v2_shown = True
            else:
                stats["avec_autre_discriminant_pumpfun"] += 1

    print("\n── Résultats du diagnostic ──")
    for k, v in stats.items():
        print(f"{k} : {v}")

    print("\n── Interprétation ──")
    if stats["avec_instruction_pumpfun"] == 0:
        print("❌ Aucune instruction Pump.fun trouvée du tout dans les 300 dernières")
        print("   signatures — soit ce wallet n'a pas créé de token récemment (activité")
        print("   plus ancienne que 300 tx), soit PUMP_FUN_PROGRAM_ID est incorrect.")
    elif stats["avec_discriminant_create"] == 0 and stats["avec_discriminant_create_v2"] == 0:
        print("⚠️  Des instructions Pump.fun existent, mais AUCUNE ne correspond aux")
        print("   discriminants 'create'/'create_v2' attendus — regarde le discriminant")
        print("   observé ci-dessus, ça peut être une autre instruction (buy/sell) ou")
        print("   un format de données différent de ce qu'on attend.")
    elif stats["avec_discriminant_create_v2"] > 0 and stats["avec_discriminant_create"] == 0:
        print("✅ Le dev utilise 'create_v2', pas 'create' — c'est la cause du bug !")
    else:
        print("✅ Des créations 'create' standard ont été trouvées — vérifie le filtre créateur.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python debug_wallet_history.py <adresse_dev>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
