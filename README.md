# Black Friday Chile — Price Hunter Bot

Bot asíncrono en **Python 3.12+** para monitorear precios en tiempo real en tiendas chilenas de tecnología y ropa, detectar **mínimos históricos reales** y atrapar **ofertas extremas / errores de precio** (`>= 70%` de descuento o precios absurdos como \$100.000 CLP $\rightarrow$ \$100 CLP) con alerta inmediata por **llamada telefónica (Twilio Voice)** y **Telegram**.

---

## Características Principales (Fase 1)

- **Scraping Híbrido Antibloqueo (`scrapers/`)**:
  - Prioriza `curl_cffi` con suplantación TLS de Chrome para parsear JSON interno (`__NEXT_DATA__`) a máxima velocidad y bajo consumo de RAM.
  - Respaldo automático con **Playwright (async)** bloqueando imágenes, fuentes y media.
  - Rotación de User-Agents, soporte para proxies rotativos, reintentos con backoff exponencial (`tenacity`) y jitter aleatorio.
  - Distinción explícita entre **Todo medio de pago** y **Exclusivo tarjeta de tienda** (ej. Tarjeta CMR en Falabella).
  - Modo **híbrido**: descubrimiento automático por categorías + watchlist de URLs específicas (`config/targets.yaml`).
- **Detector de Anomalías de Precisión Exacta (`analyzer/`)**:
  - Aritmética `Decimal` exacta (sin errores de punto flotante en el límite del `70.0%`).
  - **Arranque en frío**: si un producto es nuevo y no tiene historial previo, compara contra `normalPrice` (`>= 70%`) o dispara si `price <= $1.000 CLP` con stock.
  - **Filtros anti falsos positivos**: descarta `price <= 0`, `in_stock = False` y fallos de parseo HTML.
  - **Cooldown por producto y precio** (default `6 horas`) para evitar alertas duplicadas.
- **Alertas Multinivel (`notifier/`)**:
  - **Nivel 1 (`HISTORICAL_LOW`)**: Mensaje MarkdownV2 por Telegram con tienda, producto, precio actual, mediana 30d, `%` de descuento, medio de pago y enlace directo.
  - **Nivel 2 (`PRICE_ERROR`)**: Llamada telefónica Twilio Voice (TwiML en español, 100% asíncrona) + mensaje prioritario por Telegram ejecutados en paralelo.
  - **Rate limit global para Twilio** (default `180s` = máximo 1 llamada cada 3 minutos sin importar el producto), protegido con `asyncio.Lock` y persistido en Supabase, manteniendo siempre el envío de **todos** los mensajes de Telegram.

---

## Estructura del Proyecto

```text
bf-price-hunter/
├── config/
│   ├── settings.py          # pydantic-settings (.env)
│   └── targets.yaml         # Categorías y watchlist por tienda
├── db/
│   ├── schema.sql           # Esquema PostgreSQL listo para Supabase SQL Editor
│   ├── models.py            # Modelos SQLAlchemy 2.0 async
│   ├── session.py           # Engine asyncpg compatible con PgBouncer
│   └── repository.py        # Consultas optimizadas de historial y cooldown
├── analyzer/
│   ├── models.py            # Enums y modelos de decisión
│   └── detector.py          # PriceAnomalyDetector
├── notifier/
│   ├── base.py              # AlertPayload y formateador CLP
│   ├── telegram.py          # Telegram Bot API (MarkdownV2)
│   ├── twilio_voice.py      # Twilio Voice REST API (TwiML es-MX + rate limit global)
│   └── dispatcher.py        # Despacho paralelo Nivel 1 / Nivel 2
├── scrapers/
│   ├── base.py              # BaseScraper (curl_cffi + Playwright)
│   ├── utils.py             # parse_clp_price, UA rotation, ProxyRotator
│   ├── registry.py          # Registro de tiendas
│   └── stores/
│       ├── falabella.py     # Scraper funcional completo (Fase 1)
│       └── stubs.py         # Esqueletos para las 7 tiendas restantes (Fase 2)
├── scheduler/
│   ├── pipeline.py          # Pipeline scrape -> DB -> analyze -> notify
│   └── runner.py            # APScheduler AsyncIOScheduler
├── tests/                   # Suite pytest
├── main.py                  # Entrypoint CLI (--once, --test-alert, daemon)
├── Dockerfile               # Compatible con Playwright y Railway
└── .env.example
```

---

## 1. Configuración de Supabase (PostgreSQL)

1. Crea un proyecto gratuito en [supabase.com](https://supabase.com/) (región recomendada: **South America - São Paulo** por latencia hacia Chile).
2. Ve a **SQL Editor** $\rightarrow$ **New Query**, pega el contenido completo de [`db/schema.sql`](db/schema.sql) y presiona **Run**.
   - Esto creará las tablas `retailers`, `products`, `price_history`, `alerts_sent`, los índices optimizados y cargará las 8 tiendas chilenas.
3. Ve a **Project Settings** $\rightarrow$ **Database** $\rightarrow$ **Connection String** $\rightarrow$ pestaña **URI** (modo **Transaction Pooler**, puerto `6543`):
   ```text
   postgresql+asyncpg://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-sa-east-1.pooler.supabase.com:6543/postgres
   ```
   > El proyecto configura automáticamente `statement_cache_size=0` en `asyncpg` para compatibilidad total con PgBouncer de Supabase.

---

## 2. Configuración de Telegram Bot

1. Abre Telegram y busca a `@BotFather`.
2. Envía `/newbot`, sigue las instrucciones y guarda el **HTTP API Token** (`TELEGRAM_BOT_TOKEN`).
3. Abre el chat con tu bot recién creado y presiona **Start** (`/start`).
4. Obtén tu `TELEGRAM_CHAT_ID` abriendo en tu navegador:
   ```text
   https://api.telegram.org/bot<TU_TELEGRAM_BOT_TOKEN>/getUpdates
   ```
   Busca el campo `"chat": {"id": 123456789}` y copia ese número en `TELEGRAM_CHAT_ID`.

---

## 3. Configuración de Twilio Voice (Llamadas a Chile)

1. Crea una cuenta en [twilio.com](https://www.twilio.com/) y anota **Account SID** (`TWILIO_ACCOUNT_SID`) y **Auth Token** (`TWILIO_AUTH_TOKEN`).
2. Compra o habilita un número con capacidad **Voice** (`TWILIO_FROM_NUMBER` en formato E.164, ej. `+15551234567`).
3. **Paso crítico para Chile**: ve a **Voice** $\rightarrow$ **Settings** $\rightarrow$ **Geo Permissions** y activa **Chile (`+56`)**.
   - Si estás en cuenta *Trial*, además debes verificar tu número celular chileno (`+569XXXXXXXX`) en **Phone Numbers** $\rightarrow$ **Verified Caller IDs**.
4. Configura en `.env`:
   ```env
   TWILIO_ENABLED=true
   TWILIO_TO_NUMBER=+56912345678
   TWILIO_GLOBAL_COOLDOWN_SECONDS=180
   ```

---

## 4. Ejecución Local y Pruebas

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# Copiar variables de entorno
cp .env.example .env
# Edita .env con tus credenciales reales

# 1. Ejecutar pruebas unitarias
pytest -v

# 2. Probar tus credenciales de Telegram + llamada de Twilio con una alerta simulada
python main.py --test-alert

# 3. Ejecutar un ciclo completo único (watchlist + categorías)
python main.py --once

# 4. Iniciar el demonio continuo con APScheduler
python main.py
```

---

## 5. Despliegue en Railway (Docker)

1. Sube este repositorio a GitHub.
2. En [railway.app](https://railway.app/), crea un nuevo proyecto $\rightarrow$ **Deploy from GitHub repo**.
3. Railway detectará automáticamente el `Dockerfile` (con Playwright + Chromium preinstalados).
4. En la pestaña **Variables** del servicio, agrega todas las claves de `.env.example` (`DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWILIO_*`, `EXTREME_DISCOUNT_THRESHOLD=70`, `TWILIO_GLOBAL_COOLDOWN_SECONDS=180`, etc.).
5. Como es un worker continuo (no servidor web HTTP), no necesita dominio público. Revisa los logs en Railway para confirmar el inicio de `APScheduler`.

---

## Roadmap Fase 2

Una vez validado el flujo completo con **Falabella**, implementa el resto de las tiendas en `scrapers/stores/` heredando de `BaseScraper` y activándolas en `config/targets.yaml`:
- Paris, Ripley (con soporte para Tarjeta Cencosud / Tarjeta Ripley)
- Samsung, Xiaomi
- Nike, Adidas, Decathlon
