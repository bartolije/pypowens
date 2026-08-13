# Image de déploiement — Railway, ou n'importe quel hôte Docker (Beelink, NAS,
# Raspberry). Rien ici n'est propre à un hébergeur : les seuls réglages qui
# changent d'un endroit à l'autre passent par l'environnement.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

WORKDIR /srv

# Les dépendances d'abord, le code applicatif ensuite : l'extra [app] tire
# pandas et numpy (yfinance), qui pèsent l'essentiel du temps de construction.
# Les isoler dans leur propre couche évite de les réinstaller à chaque retouche
# de app/.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --extra app

COPY app ./app

# Le port d'écoute vient de PORT, que l'hébergeur impose ; APP_HOST doit valoir
# 0.0.0.0 pour que le routeur atteigne le conteneur — ce qui exige, côté app,
# d'avoir configuré APP_AUTH_USER / APP_AUTH_PASSWORD.
CMD ["python", "-m", "app"]
