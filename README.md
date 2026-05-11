# dialin ☕

A specialty-coffee brew journal and dial-in coach, built on AWS.
Log your beans, gear, and brews. Ask the bot for extraction advice grounded in your actual data.

---

## What it does

- **Log brews** — dose, yield, grind, time, temp, taste, rating
- **Track coffees** — roaster FK, origin, process, roast date, bag weight
- **Manage roasters** — canonical entity with city, country, website
- **Manage gear** — grinder, espresso machine, brewer, kettle
- **Track cafes & visits** — log where you've been, what you ordered, rating
- **Dial-in advice** — surfaces your best brew for a given coffee+method, computes grind delta, ratio drift, and rating trend
- **Taste preferences** — persistent memory (origins, processes, roasters, cafes, home city)
- **Cafe recommendations** — city-aware, tiered confidence, no hallucination

---

## Architecture

```
Browser  →  API Gateway (HTTP API; no JWT at edge)  →  Lambda verifies Clerk JWT →  DynamoDB
                                    ↘  Bedrock (Claude Haiku 4.5)
```

- **Infra**: Terraform — API Gateway, Lambda, DynamoDB, IAM, CloudWatch, AWS Budgets ($10/mo cap)
- **LLM**: Amazon Bedrock Converse API (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- **DB**: DynamoDB single-table design with GSI1 for brews-by-coffee and visits-by-cafe
- **UI**: Vanilla HTML + CSS + JS, served locally via `make ui`

---

## Data model

| PK | SK | Entity |
|----|----|--------|
| `USER#<id>` | `PROFILE` | User preferences |
| `USER#<id>` | `ROASTER#<id>` | Roaster |
| `USER#<id>` | `COFFEE#<id>` | Coffee bag |
| `USER#<id>` | `EQUIP#<id>` | Equipment |
| `USER#<id>` | `CAFE#<id>` | Cafe |
| `USER#<id>` | `BREW#<isoTs>#<id>` | Brew log |
| `USER#<id>` | `VISIT#<isoTs>#<id>` | Cafe visit |

GSI1: `(GSI1PK, GSI1SK)` — brews keyed by `COFFEE#<id>`, visits keyed by `CAFE#<id>`

---

## API routes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | LLM conversation with tool use |
| GET / POST | `/roasters` | List / create roasters |
| PATCH | `/roasters/{roasterId}` | Edit / retire a roaster |
| GET / POST | `/coffees` | List / create coffees |
| PATCH | `/coffees/{coffeeId}` | Edit a coffee |
| DELETE | `/coffees/{coffeeId}` | Permanently delete a coffee |
| GET / POST | `/brews` | List / log brews |
| PATCH | `/brews/{brewId}` | Edit a brew |
| DELETE | `/brews/{brewId}` | Delete a brew |
| GET / POST | `/equipment` | List / add equipment |
| PATCH | `/equipment/{equipId}` | Edit / retire equipment |
| GET / POST | `/cafes` | List / add cafes |
| PATCH | `/cafes/{cafeId}` | Edit / retire a cafe |
| GET / POST | `/visits` | List / log cafe visits |
| GET / PATCH | `/profile` | Get / update taste preferences |

---

## LLM tools

| Tool | Description |
|------|-------------|
| `search_known_roasters` | Reference list of ~70 vetted US specialty roasters |
| `add_roaster` / `list_roasters` / `update_roaster` | Roaster CRUD |
| `add_coffee` / `list_coffees` / `update_coffee` / `archive_coffee` / `delete_coffee` | Coffee CRUD |
| `add_equipment` / `list_equipment` | Equipment management |
| `log_brew` / `list_brews` / `update_brew` / `delete_brew` | Brew CRUD |
| `get_dialin_advice` | Best brew + ratio delta + grind note + trend |
| `summarize_coffee` | Avg rating, top taste words, best/last brew |
| `add_cafe` / `list_cafes` / `update_cafe` | Cafe management |
| `log_visit` / `list_visits` | Cafe visit log |
| `get_preferences` / `update_preferences` | Persistent taste profile |

---

## Quickstart

### Prerequisites

- AWS account with Bedrock model access for `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- Terraform ≥ 1.5
- Python 3.12

### Deploy

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform.tfvars with your project_name, region, etc.

cd terraform
terraform init
terraform apply
```

Applying builds the Lambda zip with **`pip install -r ../lambda/requirements.txt`** locally; you need **Python 3** and **internet** on that machine.

Copy the `api_url` output value.

### Run the UI

```bash
make ui            # serves on http://localhost:8000
# or
make ui UI_PORT=8001
```

Paste the API URL into the input at the top of the UI.

### Authentication (Clerk, optional)

For multi-user–safe auth, use **Clerk** in the UI and set **`clerk_jwt_issuer`** in Terraform so API Gateway validates session JWTs before Lambda runs. Step-by-step (Dashboard URLs, issuer, audience, local redirects) is in **[CLERK.md](./CLERK.md)**. Quick pieces:

- **Frontend:** `web/dialin-config.js` — set `clerkPublishableKey`, or `localStorage.dialin.clerkPk`.
- **Backend:** `clerk_jwt_issuer` in `terraform.tfvars` — Lambda verifies the Bearer token against Clerk JWKS (see [`CLERK.md`](./CLERK.md)).

Leave `clerk_jwt_issuer` empty to keep the legacy manual **User id** field and body/query `userId` (fine for solo local use).

---

## Development

```bash
# Tail Lambda logs
make logs

# Deploy Lambda code changes only (fast, no Terraform)
make deploy-lambda

# Full Terraform apply
make deploy
```

---

## Cost

Under typical personal use (~50 brews/month, ~20 chat turns/day):

- Bedrock (Claude Haiku 4.5): ~$0.20–0.80/month
- DynamoDB (on-demand): < $0.10/month
- Lambda + API Gateway: free tier

A `$10/month` AWS Budget alert is included in the Terraform config.
