# DataKarkhana / MarketingOstad — System Architecture Guide

> **Document Version:** 2.0  
> **Last Updated:** September 2026  
> **Architecture Pattern:** Dual-Layer Hybrid Client-Cloud Architecture  

---

## 📌 1. High-Level Architecture Overview

DataKarkhana combines a **central cloud control plane** (for user management, credit balances, license keys, and public datasets) with a **local client automation engine** (for Google Maps web scraping and WhatsApp campaigns).

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          User's PC (Desktop App)                        │
│                                                                         │
│  ┌───────────────────────┐             ┌─────────────────────────────┐  │
│  │   Electron Frontend   │             │    Local Python Backend     │  │
│  │    (Next.js / UI)     │◄───HTTP────►│       (127.0.0.1:8000)      │  │
│  └───────────┬───────────┘  (Port 8000)└──────────────┬──────────────┘  │
│              │                                        │                 │
│              │                                  Local Automation:       │
│              │                                  • Real Google Chrome    │
│              │                                  • Google Maps Scraper   │
│              │                                  • WhatsApp Web + QR     │
│              │                                  • Local SQLite DB       │
│              │                                  • Private .xlsx Files   │
└──────────────┼────────────────────────────────────────┼─────────────────┘
               │                                        │
               │ Authenticated REST API Calls           │
               │ (Bearer JWT Token / License Key)       │
               ▼                                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Central Cloud Server (Render)                      │
│                                                                         │
│  • FastAPI REST API                                                     │
│  • User Authentication & Password Hashing                               │
│  • HMAC-SHA256 Production Key Verification                              │
│  • Credit Balance & Transaction Accounting                              │
│  • bKash / Pathao Payment Verification System                           │
│  • Public Datasets Marketplace Catalog                                  │
│  • ZERO Chrome / ZERO Selenium running on the cloud (saves 100% RAM)    │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    │ Connection Pool (Port 6543)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                  Central Cloud Database (Supabase)                      │
│                                                                         │
│  • Managed PostgreSQL (Transaction Pooler)                              │
│  • Single Source of Truth for:                                          │
│    - users, licenses, credit_transactions, payment_requests             │
│    - datasets, access_logs, brevo_applications                          │
│  • Completely shielded behind the Cloud API (No direct client access)   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 2. Why This Architecture?

| Challenge | Why Cloud Server Fails | How Our Local Desktop Architecture Solves It |
| :--- | :--- | :--- |
| **Server Costs & RAM** | Headless Chrome requires 300MB–600MB RAM per instance. 10 users scraping simultaneously would crash Render or cost $100+/mo. | Chrome runs on the **user's computer** using their own RAM and CPU. Hosting cost = **৳0**. |
| **IP Blocking & Captchas** | Google Maps instantly blocks datacenter IP ranges (AWS, Render, DigitalOcean) with 429 Too Many Requests and CAPTCHAs. | Scrapers execute using the user's **residential home/office IP**, which Google trusts. |
| **WhatsApp Automation** | WhatsApp Web requires the user to scan a QR code with their mobile phone on an interactive screen. Impossible on headless cloud servers. | Local Chrome opens an interactive window on the user's PC for seamless QR code authentication. |
| **Database Security** | Bundling raw PostgreSQL credentials (`postgresql://...`) into a desktop app lets users extract the password and hack the database. | The desktop app **never** connects directly to PostgreSQL. All requests go through authenticated REST endpoints. |

---

## 🛡️ 3. Security & Data Separation Model

### The Golden Rule
> **Desktop client apps must NEVER hold the direct database connection string or database password.**

### Storage Division

```
Data Category                 Storage Location            Access Control
──────────────────────────────────────────────────────────────────────────
User Credentials              Central PostgreSQL          Cloud API Only (Hashed)
Production License Keys       Central PostgreSQL          Admin / Cloud API
Credit Balances & Billing     Central PostgreSQL          Admin Approval Only
Public Marketplace Catalog    Central PostgreSQL          Public Read / Admin Write
──────────────────────────────────────────────────────────────────────────
Private Scraped Leads         Local PC (.xlsx & SQLite)   User Only (Kept Private)
WhatsApp Session Data         Local PC                    User Only
Local Scraper Logs            Local PC                    User Only
```

1. **Central PostgreSQL (Supabase)**:
   - Stores `DATABASE_URL` **strictly** in the Render Dashboard Environment Variables.
   - It is never exposed in client git repositories, installer packages, or frontend bundles.
   - Only the Render backend connects to PostgreSQL.

2. **Local SQLite (`datakarkhana_local.db`)**:
   - Runs automatically on the user's PC when `DATABASE_URL` is omitted or unconfigured.
   - Automatically detects template placeholders (e.g. `[YOUR-PASSWORD]`) and defaults to local SQLite with zero errors.
   - Stored in the user's writable data folder (`%APPDATA%/datakarkhana/` on Windows / `~/.datakarkhana/` on Mac).
   - Zero configuration, zero passwords, zero cloud network dependency.
   - Stores private scratch jobs, local campaign histories, and offline caches.

### 🌐 Smart Dynamic Route Dispatching (`client.ts`)
The desktop client automatically routes every outgoing request to its proper destination:

| Route Namespace | Target Destination | Purpose |
| :--- | :--- | :--- |
| `/api/auth/*` | **Render Cloud Server** | Authenticates against Supabase; returns secure JWT |
| `/api/admin/*` | **Render Cloud Server** | Manages packages, customers, and payment verifications |
| `/api/datasets/*` | **Render Cloud Server** | Serves shared marketplace catalog & dataset unlocks |
| `/api/payments/*` | **Render Cloud Server** | Manages bKash/Pathao requests & credit transactions |
| `/api/license/*` | **Render Cloud Server** | Verifies production license keys and generates tokens |
| `/api/requests/*` | **Render Cloud Server** | Submits custom dataset search orders |
| `/api/scraper/*` | **Local Engine (`127.0.0.1:8000`)** | Runs Selenium Chrome using user's residential IP |
| `/api/marketing/*` | **Local Engine (`127.0.0.1:8000`)** | Runs WhatsApp Web session & local email campaigns |
| `/ws/scraper/*` | **Local Engine (`ws://127.0.0.1:8000`)** | Streams live Chromium frame previews to terminal |

### 🔄 User Profile & Credit Deduction Synchronization
1. **User Sign-in:** User logs in via the UI. The request is routed to Render Cloud, which validates credentials against Supabase and returns a signed JWT.
2. **Local Scraping Launch:** When the user launches a scrape or WhatsApp campaign, the request hits the Local Python Engine (`127.0.0.1:8000`) with the Bearer JWT.
3. **Automatic User Upsert:** The Local Engine decodes the JWT, verifies the user, queries Render `/api/auth/me` to fetch current credits, and upserts the profile into the local SQLite database.
4. **Authoritative Cloud Credit Deduction:** The Local Engine deducts credits locally and invokes the Cloud endpoint `/api/user/deduct-credits` with the user's token so that Supabase PostgreSQL balances update immediately.

---

## 💻 4. Repository & Component Structure

### Directory Map
```
Personal-projects/datakarkhana/
├── datakarkhana-frontend/       # Next.js 16 / React 19 Frontend Web App
│   ├── src/app/auth/page.tsx   # Login, Register, and OTP verification
│   ├── src/components/layout/  # TopNavbar, Sidebar with live connection dots
│   ├── src/components/shared/  # ConnectionStatusDots component
│   ├── src/lib/constants.ts    # Dual-mode API base URL resolver
│   ├── src/lib/desktop.ts      # Desktop detection hooks (isDesktopApp)
│   └── src/lib/hooks/          # useConnectionStatus real-time health hook
│
├── datakarkhana-backend/        # FastAPI / Python Scalable Backend Engine
│   ├── main.py                 # Slim application entrypoint (<250 lines), PNA/CORS middleware, re-exports
│   ├── core/                   # Security, auth dependencies, constants, PNA headers
│   │   ├── constants.py        # Path tokens (uploads, scrape_results, logs) & server uptime
│   │   ├── security.py         # JWT tokens & Argon2/Bcrypt password hashing
│   │   └── dependencies.py     # Auth dependencies (get_current_user, get_admin_user, plan checks)
│   ├── schemas/                # Strongly typed Pydantic models (Auth, Datasets, Scraper, etc.)
│   ├── services/               # Reusable business logic (Email, Scraper, Dataset, Marketing)
│   ├── routers/                # Modular API route controllers
│   │   ├── health.py           # Health checks & desktop downloads
│   │   ├── auth.py             # Registration, verification, login, credits
│   │   ├── datasets.py         # Catalog, export, sync/desync, dataset requests
│   │   ├── scraper.py          # Scraping jobs, WebSocket live stream, CSV downloads
│   │   ├── payments.py         # Gateways, bKash/Pathao requests, approvals
│   │   ├── licenses.py         # Generation, activation, renewal, status
│   │   ├── marketing.py        # WhatsApp & Email campaigns, Brevo verification
│   │   ├── admin.py            # User management, sync quotas, bans, audit trail
│   │   └── config.py           # Regional hierarchy & system metadata
│   ├── database.py             # Dual-engine DB (PostgreSQL + SQLite fallback)
│   ├── scraper.py              # Selenium Google Maps scraper engine
│   ├── senders.py              # Selenium WhatsApp Web campaign engine
│   ├── license_service.py      # HMAC-SHA256 production license validation
│   └── config.py               # Application configuration & regional datasets
│
└── datakarkhana-desktop/        # Electron Desktop Packaging & Runner
    ├── main.js                 # Auto-process spawner (Python backend + UI)
    ├── setup-dependencies.js   # Python portable runtime & pip dependency manager
    ├── preload.js              # Secure IPC bridge (electronAPI)
    └── start-desktop.bat       # 1-Click developer / local launcher
```

---

## ⚡ 5. Desktop Startup Lifecycle (Zero User Manual Work)

When a customer double-clicks **`DataKarkhana Desktop.exe`** (or `.app`):

```
User Double-Clicks Desktop Icon
               │
               ▼
[Electron Splash Screen Appears]
               │
               ├─► 1. Checks if backend is already running on port 8000
               │      • If healthy, attaches immediately (no restart delay).
               │
               ├─► 2. If not running:
               │      • Locates Python (bundled binary, virtualenv, or system).
               │      • If Python missing, auto-downloads portable runtime.
               │      • Auto-creates local template if .env is missing.
               │
               ├─► 3. Spawns Python Backend using native CreateProcess (shell: false)
               │      • Prevents Windows spaces-in-path bugs ("C:\Program Files").
               │      • Streams stdout/stderr to userData/backend.log.
               │
               ├─► 4. Polls http://127.0.0.1:8000/api/health until responsive
               │
               ▼
[Main Desktop Window Opens]
               │
               ▼
[TopNavbar & Auth Card Display Live Status Dots]
  • 🟢 Server: 8000 (Local Python Engine Connected)
  • 🟢 Database: Supabase / Local SQLite Ready
```

---

## 🔒 6. Chromium Private Network Access (PNA) & Hybrid Web-to-Local Bridge

### 🌐 What is Private Network Access (PNA)?
[Private Network Access (PNA)](https://wicg.github.io/private-network-access/) is a security specification enforced by Google Chromium (the engine powering Google Chrome, Microsoft Edge, and Electron).

Chromium categorizes network endpoints into three IP address spaces:
1. **Public Space**: Public internet domains (`https://datakarkhana-frontend.vercel.app`, Google, AWS).
2. **Private Space**: Intranet / RFC1918 IPs (`192.168.x.x`, `10.x.x.x`).
3. **Local Space**: Loopback interfaces (`127.0.0.1`, `localhost`, `::1`).

### ⚠️ The Hybrid Desktop Challenge
DataKarkhana uses a **Hybrid Desktop Architecture**:
- **Frontend UI:** Hosted on high-speed Vercel CDN (`https://datakarkhana-frontend.vercel.app`) for instant zero-reinstall updates.
- **Backend Automation Engine:** Runs locally on the customer's PC (`http://127.0.0.1:8000`) for residential IP scraping and WhatsApp automation.

When the frontend page (`Public` space over `HTTPS`) makes an API call to `http://127.0.0.1:8000` (`Local` space over `HTTP`), Chromium's sandbox considers this a **cross-space, mixed-content request** and activates strict PNA verification.

```
┌────────────────────────────────────────────────────────┐
│ PUBLIC ADDRESS SPACE (HTTPS)                           │
│ https://datakarkhana-frontend.vercel.app               │
└──────────────────────────┬─────────────────────────────┘
                           │
                           │  1. PNA Preflight (OPTIONS)
                           │  Access-Control-Request-Private-Network: true
                           ▼
┌────────────────────────────────────────────────────────┐
│ LOCAL ADDRESS SPACE (HTTP)                             │
│ http://127.0.0.1:8000 (Python FastAPI Engine)          │
└──────────────────────────┬─────────────────────────────┘
                           │
                           │  2. Preflight Response (200 OK)
                           │  Access-Control-Allow-Private-Network: true
                           │  Access-Control-Allow-Origin: vercel.app
                           ▼
┌────────────────────────────────────────────────────────┐
│ 3. Actual Request Executed (POST /api/auth/login)      │
│ Access-Control-Allow-Private-Network: true             │
└────────────────────────────────────────────────────────┘
```

### 🔁 The PNA Handshake Sequence
1. **Preflight Check:** Before sending any `POST`, `PUT`, `DELETE`, or credentialed `GET`, Chromium transmits an `OPTIONS` preflight containing:
   ```http
   OPTIONS /api/auth/login HTTP/1.1
   Host: 127.0.0.1:8000
   Origin: https://datakarkhana-frontend.vercel.app
   Access-Control-Request-Method: POST
   Access-Control-Request-Private-Network: true
   ```
2. **Server Grant:** The target server must explicitly approve the private network connection by returning:
   ```http
   HTTP/1.1 200 OK
   Access-Control-Allow-Origin: https://datakarkhana-frontend.vercel.app
   Access-Control-Allow-Credentials: true
   Access-Control-Allow-Private-Network: true
   ```
3. **Execution:** Only when this header is received will Chromium allow the browser/Electron renderer to dispatch the actual API call. If missing or if the server returns `400 Bad Request`, Chromium aborts with `ERR_NETWORK` / `TypeError: Failed to fetch`.

### 🚨 The Starlette / FastAPI Gotcha
By default in Python's Starlette / FastAPI framework:
- `CORSMiddleware` initializes with `allow_private_network: bool = False`.
- When Chromium sends `Access-Control-Request-Private-Network: true`, Starlette's preflight handler detects an unrecognized private network request and immediately aborts:
  ```http
  HTTP/1.1 400 Bad Request
  content-type: text/plain; charset=utf-8

  Disallowed CORS private-network
  ```
- Because of this `400 Bad Request`, the browser cancels the request before it reaches Python route handlers.

### 🛠️ The Complete Two-Tier Fix in DataKarkhana

#### Tier 1: Backend FastAPI Configuration (`datakarkhana-backend/main.py`)
Both `CORSMiddleware` and explicit response middleware must enable PNA:
```python
# 1. Native Starlette CORSMiddleware with allow_private_network=True
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if FRONTEND_URL else ["*"],
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
    allow_private_network=True,  # CRITICAL: Solves "Disallowed CORS private-network"
)

# 2. Defense-in-depth response middleware
@app.middleware("http")
async def private_network_access_middleware(request: Request, call_next):
    if request.method == "OPTIONS" and (
        request.headers.get("access-control-request-private-network")
        or request.headers.get("access-control-request-method")
    ):
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("origin") or "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response
```

#### Tier 2: Electron Client Environment (`datakarkhana-desktop/main.js`)
Electron's Chromium runtime must be told to allow mixed insecure content and disable strict PNA blocks:
```javascript
// 1. Chromium Command-Line Switches (before app ready)
app.commandLine.appendSwitch("disable-site-isolation-trials");
app.commandLine.appendSwitch("allow-running-insecure-content");
app.commandLine.appendSwitch("disable-features", "BlockInsecurePrivateNetworkRequests");
app.commandLine.appendSwitch("disable-web-security");

// 2. BrowserWindow WebPreferences
mainWindow = new BrowserWindow({
  webPreferences: {
    preload: path.join(__dirname, "preload.js"),
    nodeIntegration: false,
    contextIsolation: true,
    webSecurity: false,               // Bypasses browser cross-origin locks
    allowRunningInsecureContent: true, // Permits HTTPS -> HTTP 127.0.0.1 traffic
  },
});
```

---

## 🌐 7. Environment Variables Reference

### Frontend (`datakarkhana-frontend/.env`)
```env
# Mode selection: "local" (uses 127.0.0.1:8000) or "render" (uses cloud backend)
NEXT_PUBLIC_API_MODE=local

# Local Backend Endpoint
NEXT_PUBLIC_API_LOCAL=http://127.0.0.1:8000

# Production Cloud Backend Endpoint (Render)
NEXT_PUBLIC_API_RENDER=https://datakarkhana-backend.onrender.com

# Public Support Contacts
NEXT_PUBLIC_SUPPORT_EMAIL=asifdev777@gmail.com
NEXT_PUBLIC_HOTLINE_PHONE=+880 1863443343
NEXT_PUBLIC_SUPPORT_HOURS=24/7 Automated System & Live WhatsApp Assistance
```
> **Rule on Vercel:** Under Project Settings > Environment Variables, select **Config** (not `Secret`) for variables prefixed with `NEXT_PUBLIC_`.

### Backend (`datakarkhana-backend/.env`)
```env
# Cloud Server Only (Render Dashboard Environment Variables):
DATABASE_URL=postgresql://postgres.xxx:xxx@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
JWT_SECRET=marketingostad_super_secret_cyber_key_999
BREVO_API_KEY=xkeysib-xxx

# Mode Configuration
FRONTEND_MODE=local
FRONTEND_LOCAL_URL=http://localhost:3000
FRONTEND_RENDER_URL=https://datakarkhana-frontend.vercel.app

# Superadmin Seed Credentials
SUPERADMIN_EMAIL=asifdev777@gmail.com
SUPERADMIN_PASSWORD=admin123
SUPERADMIN_NAME=Asif Zaman (Superadmin)
```

---

## 🔍 8. Troubleshooting & Gotchas

### 1. `Cannot connect to backend server on port 8000`
- **Cause 1 (PNA 400 Bad Request):** Chromium sends `Access-Control-Request-Private-Network: true`. If `allow_private_network=True` is missing in FastAPI `CORSMiddleware`, FastAPI rejects the preflight with `HTTP 400 Bad Request: Disallowed CORS private-network`.  
  **Verification Command:**
  ```bash
  curl.exe -i -X OPTIONS "http://127.0.0.1:8000/api/health" \
    -H "Origin: https://datakarkhana-frontend.vercel.app" \
    -H "Access-Control-Request-Method: GET" \
    -H "Access-Control-Request-Private-Network: true"
  ```
  Must respond with `HTTP/1.1 200 OK` and `access-control-allow-private-network: true`.
- **Cause 2 (Mixed Content):** In packaged Electron, if `webSecurity: true`, Chromium refuses to execute calls from `https://` (Vercel) to `http://` (`127.0.0.1`).  
  **Fix:** `webSecurity: false` and `allowRunningInsecureContent: true` in `BrowserWindow`.
- **Cause 3 (Windows Space-in-Path):** Spawning Python inside `C:\Program Files` with `shell: true` causes Windows `cmd.exe` to fail.  
  **Fix:** Always spawn with `shell: false`.

### 2. Live Connection Status Dots
- Located in `src/components/shared/connection-status-dots.tsx`.
- Automatically polls `/api/health` every 8 seconds.
- In Electron, it uses IPC (`electronAPI.getBackendStatus()`) for zero-CORS status detection.
- Click the dots at any time to trigger an instant re-check.

---

## 🚀 8. Quick Development Commands

| Task | Command | Directory |
| :--- | :--- | :--- |
| **Run Backend Locally** | `python -m uvicorn main:app --reload --port 8000` | `datakarkhana-backend` |
| **Run Frontend Locally** | `npm run dev` | `datakarkhana-frontend` |
| **Launch Desktop App** | `npm start` (or double click `start-desktop.bat`) | `datakarkhana-desktop` |
| **Build Desktop Installer (Win)** | `npm run build:win` | `datakarkhana-desktop` |
| **Build Desktop App (Mac)** | `npm run build:mac` | `datakarkhana-desktop` |
