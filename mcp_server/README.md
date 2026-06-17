# Samsoftpay MCP Server

Lets an AI client (Claude Desktop, Claude Code, etc.) operate Samsoftpay through
its live API — create charges, check status, issue payouts/refunds, create
payment links — as approved tool calls.

## Tools
| Tool | What it does | Moves money? |
|---|---|---|
| `ping` | Health check (no key needed) | no |
| `whoami` | Show which env (test/live) + base URL | no |
| `get_charge` | Look up a charge by id | no |
| `get_payout` | Look up a payout by id | no |
| `create_charge` | Collect a Mobile Money payment | **yes** |
| `create_payout` | Send money to a phone | **yes** |
| `refund_charge` | Refund a succeeded charge | **yes** |
| `create_payment_link` | Make a shareable pay link | **yes** |

## Safety
- Money-moving tools **refuse to run with a live key** unless `SAMSOFTPAY_ALLOW_LIVE=1`.
  Default to a **test key** (`sk_test_...`) so the AI can only touch sandbox money.
- The MCP client also prompts the human to approve every tool call.
- The key is read from the environment and never logged or returned.

## Setup
```bash
cd mcp_server
pip install -r requirements.txt
```

## Configure (Claude Desktop)
Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):
```json
{
  "mcpServers": {
    "samsoftpay": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\Users\\DELL\\Desktop\\pesademo",
      "env": {
        "SAMSOFTPAY_API_KEY": "sk_test_your_test_key_here",
        "SAMSOFTPAY_BASE_URL": "https://api.samsoftpay.com"
      }
    }
  }
}
```
Restart Claude Desktop. The `samsoftpay` tools appear in the tools menu.

To allow **real money** (only when you mean it), add `"SAMSOFTPAY_ALLOW_LIVE": "1"`
and use an `sk_live_...` key. Otherwise keep a test key — the AI literally cannot
move real funds.

## Get a key
Create a merchant and copy its keys:
```
flask create-merchant "AI Automation" ai@samsoftpay.local
```
Use the `sk_test_...` value for safe AI operation.
