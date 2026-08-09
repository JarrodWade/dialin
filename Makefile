.PHONY: init plan apply destroy fmt validate ui logs test lint lint-fix lock test-chat test-coffees deploy-lambda lambda-bundle backfill-journal-rag glossary-validate gear-canonical-validate web-config eval eval-list

ARGS ?=

TF := terraform -chdir=terraform

init:
	$(TF) init

fmt:
	$(TF) fmt -recursive

validate:
	$(TF) validate

glossary-validate:
	@python3 "$(CURDIR)/scripts/validate_glossary.py"

gear-canonical-validate:
	@python3 "$(CURDIR)/scripts/validate_gear_canonical.py"

# Unit + moto integration tests (no AWS credentials or Bedrock).
VENV ?= $(CURDIR)/.venv
DEV_REQUIREMENTS ?= $(CURDIR)/requirements-dev.lock.txt
LAMBDA_REQUIREMENTS ?= $(CURDIR)/lambda/requirements.lock.txt

lock:
	uv pip compile lambda/requirements.txt -o lambda/requirements.lock.txt \
	  --python-platform manylinux2014_x86_64 --python-version 3.12
	uv pip compile requirements-dev.txt -o requirements-dev.lock.txt --python-version 3.12

lint:
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	@"$(VENV)/bin/pip" install -q -r "$(DEV_REQUIREMENTS)"
	@"$(VENV)/bin/ruff" check lambda tests evals scripts
	@"$(VENV)/bin/ruff" format --check lambda tests evals scripts

lint-fix:
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	@"$(VENV)/bin/pip" install -q -r "$(DEV_REQUIREMENTS)"
	@"$(VENV)/bin/ruff" check --fix lambda tests evals scripts
	@"$(VENV)/bin/ruff" format lambda tests evals scripts

test:
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	@"$(VENV)/bin/pip" install -q -r "$(DEV_REQUIREMENTS)"
	@"$(VENV)/bin/pytest" "$(CURDIR)/tests" -q

web-config:
	@chmod +x "$(CURDIR)/scripts/write-dialin-config.sh"
	@"$(CURDIR)/scripts/write-dialin-config.sh"

# Live prompt-quality evals: real Bedrock model + seeded scratch DynamoDB table +
# canned external-IO. Needs local AWS creds with Bedrock + DynamoDB access and
# model access enabled for the eval model. Costs cents per full run.
# Examples: make eval
#           make eval ARGS='--suite trips --reps 5'
#           make eval ARGS='--save-baseline'
eval:
	@test -d "$(VENV)" || python3 -m venv "$(VENV)"
	@"$(VENV)/bin/pip" install -q -r "$(DEV_REQUIREMENTS)"
	@cd "$(CURDIR)" && "$(VENV)/bin/python" -m evals.run_evals $(ARGS)

# List scenarios without touching AWS or the model.
eval-list:
	@cd "$(CURDIR)" && python3 -m evals.run_evals --list $(ARGS)

plan:
	$(TF) plan

apply:
	$(TF) apply

destroy:
	$(TF) destroy

UI_PORT ?= 8000
ui:
	@echo "Open http://localhost:$(UI_PORT)/"; \
	cd web && python3 -m http.server $(UI_PORT)

# Match Terraform’s Lambda zip: deps from requirements.lock.txt + *.py into lambda/build/.
lambda-bundle:
	@ROOT=$$(pwd)/lambda; rm -rf "$$ROOT/build" && mkdir -p "$$ROOT/build"; \
	  python3 -m pip install -q -r "$$ROOT/requirements.lock.txt" -t "$$ROOT/build" \
	    --platform manylinux2014_x86_64 \
	    --python-version 3.12 \
	    --implementation cp \
	    --only-binary=:all: \
	    && rm -rf "$$ROOT/build/youtube_transcript_api/test" \
	    && cp "$$ROOT"/*.py "$$ROOT/build/" \
	    && cp "$$ROOT/coffee_glossary.json" "$$ROOT/gear_canonical.json" "$$ROOT/build/" \
	    && mkdir -p "$$ROOT/build/prompts" \
	    && cp "$$ROOT"/prompts/*.md "$$ROOT/build/prompts/"

deploy-lambda: lambda-bundle
	@FUNC=$$($(TF) output -raw lambda_function_name); \
	cd lambda/build && zip -rq /tmp/dialin_lambda.zip .; \
	aws lambda update-function-code --function-name "$$FUNC" --zip-file fileb:///tmp/dialin_lambda.zip --query 'LastModified' --output text

# Rebuild Dynamo RAG chunks for brews/coffees/visits (uses local AWS credentials + Bedrock).
# Examples: make backfill-journal-rag ARGS='--dry-run'
#           make backfill-journal-rag ARGS='--user YOUR_USER_ID'
#           BEDROCK_EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0 AWS_REGION=us-east-1 ...
backfill-journal-rag:
	@TN=$$($(TF) output -raw table_name); \
	TABLE_NAME="$$TN" \
	BEDROCK_EMBEDDING_MODEL_ID="$${BEDROCK_EMBEDDING_MODEL_ID:-amazon.titan-embed-text-v2:0}" \
	python3 "$(CURDIR)/scripts/backfill_journal_rag.py" $(ARGS)

logs:
	@LG=$$($(TF) output -raw log_group); \
	aws logs tail "$$LG" --since 10m --follow

USER_ID ?= jarrod
MSG ?= Hi
test-chat:
	@URL=$$($(TF) output -raw api_endpoint); \
	curl -sS -X POST "$$URL/chat" -H 'content-type: application/json' \
		-d "{\"userId\":\"$(USER_ID)\",\"message\":\"$(MSG)\",\"history\":[]}" | jq .

test-coffees:
	@URL=$$($(TF) output -raw api_endpoint); \
	curl -sS "$$URL/coffees?userId=$(USER_ID)" | jq .
