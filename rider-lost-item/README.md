# GreenSM Rider Lost Item Agent

Voice agent: rider calls → find ride → WhatsApp the **driver** → **Pipedrive ticket after hangup**.

Rider phone is taken from the **inbound call** (no callback asked).  
Ride mock data only needs `driver_whatsapp` (plus route / ride id).

**Flow:** during the call the agent messages the driver; when the call ends it creates the Pipedrive deal (person phone = caller).

Requires **AgentDuet `1.0.0b9`** (`agentduet[nova-sonic]==1.0.0b9` in `requirements.txt`).

## Design: AI vs code (important)

Your EM’s point: **CRM / side-effects stay in code**, not in the model. This demo already follows that.

| Step | Owner | Why |
|------|--------|-----|
| Hear rider, ask clarifying questions | **AI** (Nova 2 Sonic) | Speech + dialogue |
| Extract ride id / item → call tools | **AI** | Only decides *when* and passes slots |
| Match ride in `recent_rides.json` | **Code** `lookupRecentRide` | Deterministic lookup, fuzzy GRN fix |
| WhatsApp driver | **Code** `notifyDriverLostItem` → AgentDuet send | Real API; AI must not invent “I messaged them” |
| Pipedrive deal + person + notes | **Code** `finalize_after_call` on hangup | Always after call; never model-written CRM |
| Rider phone on ticket | **Code** from call CLI (`noti.participant`) | Not asked, not hallucinated |
| Driver WA replies → deal note | **Code** `handle_incoming_message` | No LLM on WA text |

**AI must not:** invent rides, invent ticket numbers, call Pipedrive, invent WhatsApp success.

**Code owns:** ride DB, WhatsApp send result, Pipedrive create, post-call finalize.

```
Rider speech ──► Nova (talk + tool calls)
                      │
                      ▼
              tools.py (code)
                 ├─ lookupRecentRide  → JSON rides
                 └─ notifyDriverLostItem → WhatsApp now
                      │
                      ▼ hangup
              finalize_after_call() → Pipedrive (code only)
```

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --pre -r requirements.txt
# If the venv was created on an older beta, reinstall explicitly:
# pip install --pre 'agentduet[nova-sonic]==1.0.0b9'
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | yes | Voice + WhatsApp connector |
| `AWS_*` | yes | Nova 2 Sonic on Bedrock |
| `AGENTDUET_WA_SUBSCRIBER` | no* | Optional; auto-learned on first inbound WA |
| `PIPEDRIVE_API_TOKEN` | no | Real CRM (else mock) |
| `PIPEDRIVE_COMPANY_DOMAIN` | no | Deal URLs |
| `PIPEDRIVE_STAGE_ID` | no | One pipeline column for demos |

### WhatsApp setup (new users — you do **not** need the id)

AgentDuet does not publish the WhatsApp inbox id in the dashboard as a simple
“copy this for `.env`” field for every connector. The SDK exposes it as
`msg.subscriber` on the **first inbound WhatsApp**.

1. Leave `AGENTDUET_WA_SUBSCRIBER` blank in `.env`.
2. Start `python agent.py` — you’ll see a FIRST-RUN WhatsApp setup banner.
3. From any phone, send `hi` to your **business** WhatsApp (the one on the connector).
4. Log shows `READY — WhatsApp inbox id learned: …` and writes `.wa_subscriber`.
5. Now run the voice demo — driver alerts will send.

\* Optional: after step 4, paste that id into `.env` as `AGENTDUET_WA_SUBSCRIBER=…`.

**Driver phone** must also message your business WhatsApp once (Meta 24h window)
before free-text alerts arrive on that handset.

Ticket reply tracking uses an **in-memory** registry for this process only
(restart clears open-ticket → driver phone mappings).

## Run

```bash
source .venv/bin/activate
python agent.py
```

## Demo

Rides in [`data/recent_rides.json`](data/recent_rides.json):

- **GRN-88421** — Marina Bay → Changi — e.g. *black backpack*
- **GRN-88455** — Orchard → Holland Village — e.g. *mobile phone*

Say the ride / route, confirm. WhatsApp goes out during the call; the Pipedrive deal appears **after hangup** (log: `Post-call ticket ready`).

Pipedrive deal title looks like: `Lost item: black backpack (GRN-88421)`  
Person phone = caller CLI. Notes: ride summary + WhatsApp sent/failed.

Driver WA replies are logged onto the same deal as plain notes (no extra LLM).

## Layout

| File | Role |
|------|------|
| `agent.py` | `main()` only: config, triggers, register handlers |
| `handlers.py` | Inbound call attach, voice call, inbound WhatsApp |
| `nova_session.py` | Nova Sonic media bridge + tool dispatch |
| `prompts.py` | System prompt + greeting |
| `audio.py` | 24k→16k downsample + rate constants |
| `tools.py` | `lookupRecentRide` / `notifyDriverLostItem` |
| `ticket_registry.py` | In-memory driver WA → deal map |
| `phone.py` | Shared phone normalization |
| `rides.py` + `data/recent_rides.json` | Mock rides (`driver_whatsapp` only for contact) |
| `whatsapp.py` | Outbound `SendWAMessage` (docs-aligned) |
| `pipedrive.py` | Deal + notes |
| `wa_subscriber.py` | Discover / cache WA business subscriber |
