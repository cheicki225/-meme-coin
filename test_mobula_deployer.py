"""
Usage : python test_mobula_deployer.py
"""
import asyncio
import config
import mobula_client

async def main():
    address = "A4FYAoME4aPxLVuQKFwRbwxM72yAKN12izYokgfJKKmw"
    print(f"Clé Mobula configurée : {bool(config.MOBULA_API_KEY)}")
    print(f"Test sur : {address}\n")

    result = await mobula_client.get_deployer_tokens(address)
    print(f"Résultat : {result}")
    print(f"Nombre de tokens trouvés : {len(result)}")

asyncio.run(main())
