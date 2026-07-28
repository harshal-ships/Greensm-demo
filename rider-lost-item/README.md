# GreenSM Rider Lost Item Agent

Voice agent for riders reporting something left in a recent GreenSM ride.

1. Rider calls in (AgentDuet + Nova 2 Sonic)
2. Agent gathers item + ride details (no callback number asked)
3. Looks up the driver from a mock recent-rides dataset
4. Creates a **Pipedrive** deal (ticket)
5. **WhatsApps** the driver via AgentDuet — demo success = message delivered
6. If WhatsApp fails, the agent **tells the rider** (no silent failure)
7. Driver WhatsApp replies can be logged onto the same Pipedrive deal

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --pre -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

| Variable | Purpose |
|----------|---------|
| `AGENTDUET_API_KEY` / `AGENTDUET_CONNECTOR_UUID` | Connector with voice + WhatsApp |
| `AGENTDUET_WA_SUBSCRIBER` | WhatsApp **business** identity (not the TELCO voice number) |
| `AWS_*` | Nova 2 Sonic on Bedrock |
| `PIPEDRIVE_API_TOKEN` | Real Pipedrive deals (optional) |
| `PIPEDRIVE_COMPANY_DOMAIN` | e.g. `yourcompany` for deal URLs |

## Run

```bash
source .venv/bin/activate
python agent.py
```

## Demo script

Use a ride from [`data/recent_rides.json`](data/recent_rides.json):

- **GRN-88421** — Harshal — Marina Bay → Changi — item e.g. *black backpack*
- **GRN-88455** — Happy Roy — Orchard → Holland Village — item e.g. *mobile phone*

Watch logs for:

- `Tool use: lookupRecentRide` / `fileLostItemReport`
- Pipedrive deal id
- `WhatsApp sent to ... via subscriber ...`

## Layout

| File | Role |
|------|------|
| `agent.py` | AgentDuet + Nova Sonic + tools + WhatsApp |
| `tools.py` | `lookupRecentRide` / `fileLostItemReport` |
| `rides.py` + `data/recent_rides.json` | Mock recent rides |
| `pipedrive_client.py` | Create deal + notes (or mock) |
