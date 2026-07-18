variable "project_name" {
  description = "Short name used to prefix all created resources."
  type        = string
  default     = "dialin"
}

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID. Claude Haiku 4.5 via the US cross-region inference profile gives best-in-class tool-use + strong world knowledge for cafe recommendations, at very low cost."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_embedding_model_id" {
  description = <<-EOT
    Bedrock embedding model for journal RAG (retrieve_journal tool), e.g. amazon.titan-embed-text-v2:0.
    Set to empty string to disable embedding sync and semantic search. Enable model access in Bedrock console.
  EOT
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "max_output_tokens" {
  description = "Hard cap on tokens generated per response."
  type        = number
  default     = 600
}

variable "max_tool_iterations" {
  description = "Maximum tool-use loop iterations per chat turn (safety cap). Larger because the bot may chain: list_coffees -> list_equipment -> summarize_coffee -> get_preferences."
  type        = number
  default     = 12
}

variable "chat_max_web_searches" {
  description = "Max search_web calls per chat turn (safety cap against runaway trip-discovery searches hitting the 30s Lambda/API Gateway ceiling). 0 disables the cap."
  type        = number
  default     = 4
}

variable "chat_history_turn_limit" {
  description = "Max chat messages (USER+BOT) sent to Bedrock per turn; keep in sync with web dialin-config.js chatHistoryTurnLimit."
  type        = number
  default     = 24
}

variable "chat_message_max_chars" {
  description = "Reject a single /chat message longer than this many characters (cost/abuse guard). 0 disables the check."
  type        = number
  default     = 8000
}

variable "chat_history_max_chars" {
  description = "Max total characters of chat history sent to Bedrock per turn, trimmed oldest-first on top of chat_history_turn_limit (guards against many large messages in the window). 0 disables the check."
  type        = number
  default     = 40000
}

variable "bedrock_prompt_caching" {
  description = "Enable Bedrock prompt caching (cachePoint) for the static system prompt + tool specs. Disable only for models that do not support cachePoint blocks."
  type        = bool
  default     = true
}

variable "journal_snapshot_max_items" {
  description = "Max coffees/roasters/equipment listed per category in the per-turn journal snapshot (cost/latency cap for heavy users). 0 disables the cap."
  type        = number
  default     = 20
}

variable "journal_rag_max_chunks" {
  description = "Max journal RAG chunks scanned per retrieve_journal query (pagination cap for latency)."
  type        = number
  default     = 2000
}

variable "chat_daily_limit_per_user" {
  description = "Max POST /chat turns per user per UTC day (0 = unlimited). Enforced in Lambda."
  type        = number
  default     = 0
}

variable "api_throttle_rate_limit" {
  description = "API Gateway steady-state requests per second (account-wide on this stage)."
  type        = number
  default     = 50
}

variable "api_throttle_burst_limit" {
  description = "API Gateway burst capacity for this stage."
  type        = number
  default     = 100
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda function."
  type        = number
  default     = 7
}

variable "monthly_budget_usd" {
  description = "AWS Budgets monthly cost cap for the project (USD)."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Email address that receives AWS Budgets alerts."
  type        = string
}

variable "tavily_api_key" {
  description = "Tavily API key for live web search in cafe recommendations. Get a free key at https://tavily.com. Set to empty string to disable web search."
  type        = string
  default     = ""
  sensitive   = true
}

variable "websearch_cache_ttl_seconds" {
  description = "DynamoDB TTL for shared Tavily query cache entries (identical normalized queries reuse results)."
  type        = number
  default     = 86400
}

variable "websearch_monthly_limit_per_user" {
  description = "Max live Tavily calls per userId per UTC month (cache hits do not count). Set 0 for unlimited."
  type        = number
  default     = 300
}

variable "log_trip_websearch" {
  description = "When true, Lambda logs search_web query + result titles for trip-discovery turns (LOG_TRIP_WEBSEARCH). No user message bodies."
  type        = bool
  default     = false
}

variable "chat_local_timezone" {
  description = <<-EOT
    IANA timezone used only when resolving relative dates and the client's browser omitted clientTimezone,
    AND the Dynamo profile omits timezone. Defaults to UTC. Normal users rely on Intl (web) plus optional profile override.
  EOT
  type        = string
  default     = "UTC"
}

variable "cors_allowed_origins" {
  description = "Origins for CORS allow_origins and Clerk azp validation. Use [\"*\"] for dev; restrict to your real domain(s) for production."
  type        = list(string)
  default     = ["*"]
}

variable "clerk_jwt_issuer" {
  description = "Clerk Frontend API URL (same as JWT ``iss``). When set, Lambda verifies ``Authorization`` JWTs via Clerk JWKS; client ``userId`` is rejected. Leave empty for legacy manual user id."
  type        = string
  default     = ""
}

variable "clerk_jwt_audience" {
  description = "Unused — JWT verification runs in Lambda (no API Gateway JWT authorizer; Clerk session tokens often omit ``aud``). Ignored."
  type        = string
  default     = ""
}

variable "enable_chat_streaming" {
  description = <<-EOT
    Deploy the SSE streaming chat endpoint (POST /chat/stream) as a second Lambda
    function fronted by a Lambda Function URL in RESPONSE_STREAM mode, using the
    AWS Lambda Web Adapter layer (Python has no native response streaming support).
    Off by default: this is newer infra than the buffered POST /chat path and pulls
    in a third-party layer — review terraform/lambda_stream.tf before enabling.
  EOT
  type        = bool
  default     = false
}

variable "lambda_web_adapter_layer_arn" {
  description = <<-EOT
    Lambda Web Adapter layer ARN for this region (x86_64). Only used when
    enable_chat_streaming = true. Defaults to AWS's published layer for the
    configured region; override to pin a specific version or use arm64 (see
    https://aws.github.io/aws-lambda-web-adapter/getting-started/zip-packages.html).
  EOT
  type        = string
  default     = ""
}
