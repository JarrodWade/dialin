.PHONY: init plan apply destroy fmt validate ui logs test-chat test-coffees

TF := terraform -chdir=terraform

init:
	$(TF) init

fmt:
	$(TF) fmt -recursive

validate:
	$(TF) validate

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
