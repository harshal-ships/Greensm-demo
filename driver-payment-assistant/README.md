# GreenSM Driver Payment Voice Agent

Demo voice agent for GreenSM. When a driver calls in and asks something like **"When do I get paid?"**, the agent answers from GreenSM’s payment policy only and speaks the reply in real time.

## Stack

- **[AgentDuet](https://pypi.org/project/agentduet/1.0.0b9/)** `1.0.0b9` — incoming call + bidirectional PCM audio
- **Amazon Nova 2 Sonic** (`amazon.nova-2-sonic-v1:0`) — speech-to-speech on Bedrock
- **Ground truth:** [`policy/greensm_driver_payment_policy.md`](policy/greensm_driver_payment_policy.md)


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
| `AGENTDUET_API_KEY` | AgentDuet API key |
| `AGENTDUET_CONNECTOR_UUID` | Connector UUID for your phone line |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Bedrock credentials with Nova 2 Sonic access |
| `AWS_REGION` | Default `us-east-1` |

## Run

```bash
source .venv/bin/activate
python agent.py
```

Call your AgentDuet number and ask:

- *When do I get paid?*
- *What’s the Instant Cash-Out fee?*
- *Do you take a cut of tips?*

The agent should answer using only the policy document.

## Layout

| File | Role |
|------|------|
| `agent.py` | Full demo — AgentDuet call handling + Nova 2 Sonic bridge + policy prompt |
| `policy/greensm_driver_payment_policy.md` | Payment policy ground truth |
