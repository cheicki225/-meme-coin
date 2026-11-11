# Installation

Prérequis : Python 3.11 ou plus récent et un accès RPC Solana. Dans un environnement virtuel :

```sh
python -m venv .venv
# Windows : .venv\Scripts\activate
# Linux/macOS : source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copiez `.env.example` vers `.env`, configurez les clés d'API nécessaires et gardez `TRADING_MODE=PAPER`. Configurez une allowlist Telegram avant d'utiliser le bot. Lancez `python main.py`.

Le mode LIVE n'est pas considéré prêt pour des fonds réels. Il exige une revue de sécurité, un wallet dédié et une validation on-chain indépendante.
