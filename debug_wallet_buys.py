"""
Usage : python debug_wallet_buys.py --wallet <adresse> [--scan 300]

Montre, pour chaque transaction touchant Pump.fun dans les N dernières
signatures du wallet, si elle est comptée comme un achat ou non, et
pourquoi — pour diagnostiquer les écarts avec des outils externes (Axiom,
GMGN) plutôt que de deviner.
"""
import argparse
import asyncio

import config
import wallet_history


async def diagnose(wallet_address: str, max_scan: int = 300):
    print(f"🔍 Diagnostic complet pour : {wallet_address}")
    print(f"   Scan des {max_scan} dernières signatures...\n")

    signatures = await wallet_history._get_signatures(wallet_address, limit=max_scan)
    print(f"📄 {len(signatures)} signature(s) récupérée(s) au total.\n")

    involves_pumpfun_count = 0
    counted_as_buy = 0

    for i, sig_info in enumerate(signatures):
        tx = await wallet_history._get_raw_transaction(sig_info["signature"])
        if not tx:
            print(f"[{i+1}] {sig_info['signature'][:12]}... → ⚠️ transaction introuvable/erreur réseau")
            continue

        involves_pf = wallet_history._transaction_involves_program(tx, config.PUMP_FUN_PROGRAM_ID)
        if not involves_pf:
            continue  # on ignore silencieusement les tx qui ne touchent pas Pump.fun du tout, comme le vrai code

        involves_pumpfun_count += 1

        mint, tokens_received = wallet_history._find_token_balance_increase(tx, wallet_address, return_amount=True)
        sol_spent = wallet_history._find_native_sol_spent(tx, wallet_address)

        if mint:
            counted_as_buy += 1
            status = "✅ COMPTÉ COMME ACHAT"
        else:
            status = "❌ IGNORÉ (pas d'augmentation de solde token détectée pour ce wallet)"

        print(f"[{i+1}] {sig_info['signature'][:16]}... — {status}")
        print(f"      Touche Pump.fun : Oui")
        print(f"      Mint détecté     : {mint or 'aucun'}")
        print(f"      Tokens reçus     : {tokens_received if tokens_received else 'N/A'}")
        print(f"      SOL dépensé      : {sol_spent if sol_spent else 'N/A (ou 0 — transfert/airdrop possible)'}")
        print()

    print(f"{'='*60}")
    print(f"📊 RÉSUMÉ")
    print(f"{'='*60}")
    print(f"Signatures scannées                    : {len(signatures)}")
    print(f"Transactions touchant Pump.fun          : {involves_pumpfun_count}")
    print(f"Comptées comme achats par notre bot      : {counted_as_buy}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--scan", type=int, default=300)
    args = parser.parse_args()

    if not config.HELIUS_API_KEY:
        print("❌ Aucune clé HELIUS_API_KEY configurée.")
        return

    await diagnose(args.wallet, max_scan=args.scan)


if __name__ == "__main__":
    asyncio.run(main())
