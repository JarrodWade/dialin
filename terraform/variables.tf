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

variable "max_output_tokens" {
  description = "Hard cap on tokens generated per response."
  type        = number
  default     = 600
}

variable "max_tool_iterations" {
  description = "Maximum tool-use loop iterations per chat turn (safety cap). Larger because the bot may chain: list_coffees -> list_equipment -> summarize_coffee -> get_preferences."
  type        = number
  default     = 8
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
