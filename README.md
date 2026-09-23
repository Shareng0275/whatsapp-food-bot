# WhatsApp Food Ordering Bot — Production Hardened

A production-ready WhatsApp food-ordering bot built with Flask, Twilio WhatsApp API, SQLAlchemy, and Redis. Features an intelligent recommendation engine, server-side promo calculations, robust rate limiting, HMAC-SHA256 payment verification with idempotency, structured JSON logging with correlation tracing, and multi-worker session persistence.

---

## 🏗️ Architecture

The application is architected around transport-agnostic decoupled layers:

```
                  ┌─────────────────────────────────────┐
                  │    WhatsApp / Twilio Cloud Webhook  │
                  └──────────────────┬──────────────────┘
                                     │ POST /whatsapp (TwiML + Signature)
                                     ▼
                  ┌─────────────────────────────────────┐
                  │      Flask Adapter (bot.py)         │
                  │  - Twilio RequestValidator          │
                  │  - Multi-tier Sliding Rate Limiting │
                  │  - Correlation ID Tracing           │
                  │  - PII Masking & Safe XML Escaping  │
                  └──────────────────┬──────────────────┘
                                     │
           ┌─────────────────────────┼─────────────────────────┐
           ▼                         ▼                         ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│  Session Store      │   │  Conversation Engine│   │  Payment Gateway    │
│  (Redis / Memory)   │   │  (conversation.py)  │   │  (POST /payment/...)│
│  - Multi-worker TTL │   │  - State Machine    │   │  - HMAC-SHA256 sig  │
│  - Sliding Limiter  │   │  - Recommender      │   │  - Replay check     │
└─────────────────────┘   └──────────┬──────────┘   │  - Idempotency      │
                                     │              └─────────────────────┘
                                     ▼
                          ┌─────────────────────┐
                          │  Persistence Layer  │
                          │  (repository.py)    │
                          │  - PostgreSQL / DB  │
                          │  - Atomic TX updates│
                          └─────────────────────┘
```

---

## ⚙️ Environment Configuration

Configuration is managed dynamically via environment variables with strict validation rules between Development, Testing, and Production environments.

### Setting Up `.env`

Copy the example template:
```bash
cp .env.example .env
```

### Key Environment Variables

| Variable | Required in Prod | Default (Dev) | Description |
|---|---|---|---|
| `FLASK_ENV` / `APP_ENV` | No | `development` | `development`, `testing`, or `production`. |
| `SECRET_KEY` | **Yes** | `dev-insecure...` | 32+ character random secret for signing tokens. |
| `DATABASE_URL` | **Yes** | `sqlite:///dev.db` | PostgreSQL connection URL (e.g. `postgresql://user:pass@host:5432/db`). |
| `SESSION_STORE_TYPE` | No | `memory` | `redis` (recommended for production) or `memory`. |
| `REDIS_URL` | If Redis | `redis://localhost:6379/0` | Connection string to Redis instance or cluster. |
| `TWILIO_ACCOUNT_SID` | **Yes** | `""` | Twilio Account SID (must start with `AC`). |
| `TWILIO_AUTH_TOKEN` | **Yes** | `""` | Twilio Auth Token for webhook signature validation. |
| `TWILIO_WHATSAPP_NUMBER`| **Yes** | `""` | Twilio sender (e.g. `whatsapp:+14155238886`). |
| `TWILIO_VALIDATE_SIGNATURE`| No | `true` | Must be `true` in production; set `false` only for offline testing. |
| `PAYMENT_WEBHOOK_SECRET`| **Yes** | `""` | HMAC-SHA256 secret for verifying payment gateway callbacks. |
| `WEBHOOK_RATE_LIMIT` | No | `60` | Max requests per minute per IP on `/whatsapp`. |
| `USER_MESSAGE_RATE_LIMIT`| No | `20` | Max messages per minute per phone number. |
| `PAYMENT_INITIATION_LIMIT`| No | `3` | Max payment initiation / retry attempts per order (5 min window). |
| `PROMO_ATTEMPT_LIMIT` | No | `5` | Max invalid promo attempts before lockout. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## 🚀 Quick Start & Local Setup

### 1. Prerequisites
- Python 3.10+
- PostgreSQL (or SQLite for local dev)
- Redis (optional for local dev, recommended for production)

### 2. Install Dependencies
```bash
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt  # Or install flask twilio sqlalchemy psycopg2-binary redis pytest pytest-cov
```

### 3. Initialize Database
To seed the database with menu items and initial promo codes:
```bash
python db_init.py
```

### 4. Interactive Simulation (CLI)
You can test the entire food ordering flow directly in your terminal without any external credentials:
```bash
# Interactive mode (type messages yourself)
python simulate.py

# Scripted automatic demo
python simulate.py --auto
```

### 5. Running the Webhook Locally
```bash
# Set development environment
$env:FLASK_ENV="development"
$env:TWILIO_VALIDATE_SIGNATURE="false"  # Allows local cURL / Postman testing
python bot.py
```

---

## 🛡️ Production Deployment

### 1. Database Setup (PostgreSQL)
Create the PostgreSQL database and user:
```sql
CREATE DATABASE whatsapp_food_bot;
CREATE USER food_bot_user WITH ENCRYPTED PASSWORD 'secure_db_password';
GRANT ALL PRIVILEGES ON DATABASE whatsapp_food_bot TO food_bot_user;
```

Export the production database URL:
```bash
export DATABASE_URL="postgresql://food_bot_user:secure_db_password@postgres-host:5432/whatsapp_food_bot"
python db_init.py
```

### 2. Redis Setup
Ensure Redis is running for session storage and sliding-window rate limiting:
```bash
export SESSION_STORE_TYPE="redis"
export REDIS_URL="redis://:secure_redis_password@redis-host:6379/0"
```

### 3. Twilio Console Setup
1. In the [Twilio Console](https://console.twilio.com/), configure your WhatsApp Sandbox or Production Number.
2. Set the Webhook URL:
   - **URL**: `https://yourdomain.com/whatsapp`
   - **HTTP Method**: `POST`
3. Export Twilio credentials:
```bash
export TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export TWILIO_AUTH_TOKEN="your_actual_auth_token"
export TWILIO_WHATSAPP_NUMBER="whatsapp:+14155238886"
export TWILIO_VALIDATE_SIGNATURE="true"
```

### 4. Payment Gateway Webhook Setup
1. Configure your payment gateway webhook URL: `https://yourdomain.com/payment/webhook`
2. Export your shared secret:
```bash
export PAYMENT_WEBHOOK_SECRET="your_strong_random_webhook_secret_key"
```

### 5. Production Startup Command
Run with a production WSGI server (e.g. `gunicorn` with multiple workers behind Nginx/ALB):
```bash
gunicorn -w 4 -b 0.0.0.0:5000 --timeout 30 "bot:create_app()"
```

Ensure your reverse proxy forwards standard HTTPS proxy headers:
```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
```

---

## 🧪 Testing & Verification

Run the entire automated test suite with coverage report:

```bash
python -m pytest -v --cov=. --cov-report=term-missing
```

### Test Suites Included:
- **`test_final_regression.py`**: Full 21-step lifecycle, invariant verification, and failure boundaries.
- **`test_config.py`**: Configuration validation, production invariants, safe dict serialization.
- **`test_rate_limiting.py`**: Webhook IP limits, per-user limits, burst sliding window, promo attempt limits, payment retry limits.
- **`test_logging_monitoring.py`**: Structured JSON logging, correlation IDs, PII masking, secret redaction, health checks.
- **`test_resilience.py`**: Database outages, Redis fallbacks, payment provider timeouts, duplicate callbacks, server restarts.
- **`test_security.py`**: Twilio signature validation, SQL injection safety, XSS/XML injection safety, HMAC verification, replay protection.
- **`test_conversation_suite.py`**: Transport-agnostic conversation state machine.
- **`test_recommender.py`**: Menu filtering, history boosts, rating thresholds.
- **`test_promo.py`**: Server-side promo calculation and minimum thresholds.
- **`test_repository.py`**: ORM repository CRUD, atomic transactions, duplicates.
- **`test_bot.py`**: Flask/Twilio adapter webhook endpoints.
- **`test_simulate.py`**: Interactive and automated CLI harness.

---

## 🔧 Operational Troubleshooting & Rollback

### Troubleshooting Common Issues

| Symptom | Probable Cause | Action |
|---|---|---|
| **`403 Forbidden` on `/whatsapp`** | Invalid / missing `X-Twilio-Signature` or incorrect `TWILIO_AUTH_TOKEN`. | Check Twilio Auth Token in `.env`; verify reverse proxy is sending `X-Forwarded-Proto: https` so public URL matches. |
| **`429 Too Many Requests` on `/whatsapp`** | Rate limit threshold exceeded by IP or user spamming. | Check structured logs for `ip_rate_limit_exceeded` or `user_rate_limit_exceeded`. Adjust `WEBHOOK_RATE_LIMIT` if legitimate traffic burst. |
| **`503 Service Unavailable` on `/health`** | PostgreSQL database connection unreachable. | Verify PostgreSQL host, credentials, and connection pool limits (`pool_pre_ping=True`). |
| **`400 Bad Request: Webhook timestamp expired`** | Callback timestamp drifted beyond 300 seconds. | Check server NTP time synchronization with payment gateway. |

### Rollback Procedure

In the event of an unexpected regression or deployment blocker:
1. **Container / Instance Rollback**: Roll back the container image tag or deployment git commit to the previous stable release:
   ```bash
   git checkout <previous_stable_tag>
   pip install -r requirements.txt
   gunicorn -w 4 -b 0.0.0.0:5000 "bot:create_app()"
   ```
2. **Database Schema Backward Compatibility**: Schema additions (columns/tables) are non-destructive and backward compatible with previous releases.
3. **Session State Durability**: Active user sessions in Redis remain valid across worker restarts without data loss.

### Database Backup & Disaster Recovery

Run scheduled automated backups using PostgreSQL `pg_dump`:
```bash
# Automated daily backup:
pg_dump -U food_bot_user -h <db-host> -d whatsapp_food_bot -F c -b -v -f "/var/backups/whatsapp_food_bot_$(date +%Y%m%d).dump"

# Point-in-time restore:
pg_restore -U food_bot_user -h <db-host> -d whatsapp_food_bot -v "/var/backups/whatsapp_food_bot_YYYYMMDD.dump"
```
