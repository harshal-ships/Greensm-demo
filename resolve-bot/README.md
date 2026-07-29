# GreenSM Resolve Bot

Voice support for **riders and drivers**: lost items and complaints.  
Pipedrive ticket created **after the call ends**. No WhatsApp in this demo.

Caller phone = inbound call CLI (no callback asked).

Requires **AgentDuet `1.0.0b9`** (`agentduet[nova-sonic]==1.0.0b9` in `requirements.txt`).

## Supported cases

| Caller | Lost | Complaint categories |
|--------|------|----------------------|
| **Rider** | Item left in car (phone, bag, wallet, keys) | `rude_driver`, `unsafe_driving`, `dirty_vehicle`, `bad_route`, `refused_stop` |
| **Driver** | — | `abusive_rider`, `vehicle_damage` |

Out of scope: payment/fare disputes, no-shows, WhatsApp, found-item-by-driver.

## Design: AI vs code

| Step | Owner |
|------|--------|
| Dialogue, role/intent, slot collection | **AI** (Nova 2 Sonic) |
| Ride lookup | **Code** `lookupRecentRide` |
| Case registration | **Code** `registerCase` (queues for hangup) |
| Pipedrive deal | **Code** `finalize_after_call` on hangup |
| Caller phone on ticket | **Code** from call CLI |

```
Caller speech ──► Nova (talk + tool calls)
                      │
                      ▼
              tools.py (code)
                 ├─ lookupRecentRide  → JSON rides
                 └─ registerCase      → queue case
                      │
                      ▼ hangup
              finalize_after_call() → Pipedrive (code only)
```

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | yes | Voice connector |
| `AWS_*` | yes | Nova 2 Sonic on Bedrock |
| `PIPEDRIVE_API_TOKEN` | no | Real CRM (else mock) |
| `PIPEDRIVE_COMPANY_DOMAIN` | no | Deal URLs |
| `PIPEDRIVE_LOST_STAGE_ID` | no | Pipeline stage for lost-item deals |
| `PIPEDRIVE_COMPLAINT_STAGE_ID` | no | Pipeline stage for complaint deals |
| `PIPEDRIVE_STAGE_ID` | no | Fallback if role-specific stage unset |

## Run

```bash
source .venv/bin/activate
python agent.py
```

## Demo scripts

Rides in [`data/recent_rides.json`](data/recent_rides.json): **G88421**, **G88455**.

1. **Rider lost** — “I'm a rider. I left my black backpack on ride G 88421.”  
   → Lost deal after hangup (`Post-call Pipedrive ticket created … case_type=lost`).

2. **Rider complaint** — “I'm a rider. The driver was rude on ride 88455.”  
   → Complaint deal (`case_type=complaint`, category `rude_driver`).

3. **Driver complaint** — “I'm a driver. A rider spilled a drink and damaged the seat, ride 88421.”  
   → Complaint deal with `caller_role=driver`, category `vehicle_damage`.

## Layout

| File | Role |
|------|------|
| `agent.py` | `main()` only: config, triggers, voice handler |
| `handlers.py` | Inbound call attach + voice call |
| `nova_session.py` | Nova Sonic media bridge + tool dispatch |
| `prompts.py` | System prompt + greeting |
| `audio.py` | 24k→16k downsample + rate constants |
| `tools.py` | `lookupRecentRide` / `registerCase` |
| `rides.py` + `data/recent_rides.json` | Mock rides |
| `pipedrive.py` | Deal + notes (lost vs complaint stages) |
| `phone.py` | Phone normalization (shared utility) |
