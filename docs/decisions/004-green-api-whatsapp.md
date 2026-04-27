# 004: Green API for WhatsApp Integration

## Date
2026-03-16

## Status
accepted (supersedes earlier ManyChat consideration)

## Context
The Training Agent needs to send WhatsApp notifications to training groups and provide an AI assistant ("მრჩეველი") in WhatsApp. Options considered:
1. **ManyChat** — chatbot platform with WhatsApp integration
2. **WhatsApp Business API** (official Meta API) — requires business verification
3. **Green API** — third-party WhatsApp Web wrapper with REST API

## Decision
Use **Green API** for all WhatsApp messaging.

Configuration:
- `GREEN_API_INSTANCE_ID` and `GREEN_API_TOKEN` env vars
- QR code-based WhatsApp Web connection
- Group chat IDs: `WHATSAPP_GROUP1_ID`, `WHATSAPP_GROUP2_ID`
- Implementation: `tools/integrations/whatsapp_sender.py`

## Reasoning
1. **ManyChat rejected**: Too complex for the use case. ManyChat is designed for marketing chatbots, not programmatic messaging. The user explicitly rejected this approach.
2. **Official WhatsApp Business API rejected**: Requires Meta business verification process, template message approval, and is expensive for low-volume use. Overkill for 2 training groups.
3. **Green API chosen**: Simple REST API, instant setup via QR code, supports both private and group messages, affordable, and provides webhooks for incoming messages (needed for მრჩეველი assistant).

## Consequences
- **Positive**: Simple setup, fast to implement, supports all needed features (group messages, private messages, incoming webhooks)
- **Negative**: QR code connection may need periodic re-authentication. Third-party dependency — less reliable than official API.
- **Risk**: WhatsApp may block unofficial API connections. Mitigation: Green API has been stable, and the message volume is very low (a few messages per lecture).
- **Alternative ready**: If Green API fails, can switch to official WhatsApp Business API with template messages.
