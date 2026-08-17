"""
Usage : python debug_helius_transfers.py
"""
import asyncio
import aiohttp
from aiohttp.resolver import ThreadedResolver
import config


async def main():
    if not config.HELIUS_API_KEY:
        print("❌ HELIUS_API_KEY vide.")
        return

    url = f"https://mainnet.helius-rpc.com/?api-key={config.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransfersByAddress",
        "params": [
            "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
            {
                "mint": config.SOL_MINT,
                "direction": "out",
                "filters": {"amount": {"gte": 1520000000, "lte": 1540000000}},
                "limit": 10,
            },
        ],
    }

    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            print("Statut HTTP:", resp.status)
            text = await resp.text()
            print("Réponse brute complète:")
            print(text)


if __name__ == "__main__":
    asyncio.run(main())
