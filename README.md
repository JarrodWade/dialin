# dialin

A specialty-coffee **brew journal & dial-in coach** chatbot. You log bags
and brews; the bot reads your history and gives concrete next-attempt
advice.

Stack:

- **API Gateway (HTTP API)** -> **Lambda (Python 3.12)** -> **DynamoDB** + **Bedrock**
- **Bedrock Converse API with tool use** &mdash; the model calls Python
  functions (`add_coffee`, `log_brew`, `list_brews`, `get_dialin_advice`, ...) which
  read/write DynamoDB.
- All infra is **Terraform**-managed; budget-capped at `$10/month`.

## Architecture

```
browser (web/index.html)
        |
        v
API Gateway (HTTP API)
        |
        v
Lambda (Python: handler -> bedrock tool loop -> tools -> ddb)
   |              \
   |               \--- Bedrock (amazon.nova-lite-v1:0)
   v
DynamoDB single table  (PK / SK + GSI1)
```

### DynamoDB item shapes

| itemType | PK             | SK                          | Notes                                     |
| -------- | -------------- | --------------------------- | ----------------------------------------- |
| `Coffee` | `USER#<id>`    | `COFFEE#<coffeeId>`         | one bag of beans; tracks `gramsRemaining` |
| `Brew`   | `USER#<id>`    | `BREW#<isoTs>#<brewId>`     | time-ordered; also on GSI1 by coffee      |

### GSI1 &mdash; brews by coffee

```
GSI1PK = COFFEE#<coffeeId>
GSI1SK = BREW#<isoTs>#<brewId>
```

Lets the bot pull "all brews for *this* coffee" without scanning.

## API routes

| Method | Path                          | Description                                   |
| ------ | ----------------------------- | --------------------------------------------- |
| POST   | `/chat`                       | Conversational entrypoint (tool-calling LLM)  |
| GET    | `/coffees?userId=`            | List coffees                                  |
| POST   | `/coffees`                    | Add a coffee bag                              |
| PATCH  | `/coffees/{coffeeId}`         | Update / archive                              |
| GET    | `/brews?userId=&coffeeId=&method=` | List brews (filterable)                  |
| POST   | `/brews`                      | Log a brew (atomically decrements stock)      |

## LLM tools

Defined in `lambda/tools.py` and surfaced to Bedrock via the Converse API:

- `add_coffee`, `archive_coffee`, `list_coffees`
- `log_brew`, `list_brews`
- `get_dialin_advice` &mdash; pulls recent brews + applies extraction heuristics

## One-time setup

Same as for the billing-bot sibling repo:

1. AWS account + IAM user with `AdministratorAccess`.
2. `aws configure` so `aws sts get-caller-identity` works.
3. Bedrock model access auto-enables on first invoke for Amazon Nova models.

## Deploy

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# edit terraform.tfvars: budget_alert_email, etc.

make init
make apply
```

## Use it

```bash
make ui                          # serves UI on http://localhost:8000
```

Paste the `api_endpoint` from the Terraform output into the **API** field.

### Things to try

- "Add a bag: Sey, Wote Ethiopia, washed, roasted 2026-04-14, 250g"
- "Log a brew: V60, 15g in, 250g out, 3:10, grind 18, sour"
- "Give me dial-in advice for that"
- "Show my V60 brews this week"

## Cost guardrails

- DynamoDB on-demand
- Lambda 512MB / 30s / `MAX_OUTPUT_TOKENS=400` / `MAX_TOOL_ITERATIONS=5`
- CloudWatch log retention 7d
- AWS Budget at `$10/month` with 50/80/100% alerts

## Tear down

```bash
make destroy
```
