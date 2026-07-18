# Streaming chat endpoint (POST /chat/stream) — optional, see enable_chat_streaming.
#
# Python Lambda has no native response-streaming support (only Node.js managed
# runtimes do), so this uses the AWS Lambda Web Adapter layer to run
# lambda/stream_server.py (a plain stdlib HTTP server) as a subprocess and proxy
# it in RESPONSE_STREAM mode. Shares the exact same code bundle as the buffered
# API Lambda (terraform/lambda.tf) — only the handler, layer, and a few env vars
# differ — so app-level logic (auth, tool loop, prompts) can never drift between
# the two endpoints.
#
# Auth: identical to the buffered API (Clerk JWT verified in-process via
# auth.py, or legacy client-supplied userId). Lambda Function URLs have no
# built-in JWT authorizer, so authorization_type = "NONE" is intentional here —
# the same as API Gateway's routes, which also use authorization_type = "NONE"
# and verify auth inside the Lambda.

locals {
  lambda_web_adapter_layer_arn = (
    trimspace(var.lambda_web_adapter_layer_arn) != "" ?
    trimspace(var.lambda_web_adapter_layer_arn) :
    "arn:aws:lambda:${var.region}:753240598075:layer:LambdaAdapterLayerX86:28"
  )
}

resource "aws_cloudwatch_log_group" "lambda_chat_stream" {
  count             = var.enable_chat_streaming ? 1 : 0
  name              = "/aws/lambda/${var.project_name}-chat-stream"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "chat_stream" {
  count            = var.enable_chat_streaming ? 1 : 0
  function_name    = "${var.project_name}-chat-stream"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "run.sh"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  # Function URLs allow up to 15 min; give the tool loop real headroom instead of
  # the 30s API Gateway ceiling the buffered endpoint is stuck with.
  timeout     = 120
  memory_size = 512
  layers      = [local.lambda_web_adapter_layer_arn]

  environment {
    variables = merge(local.lambda_common_env, {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/bootstrap"
      AWS_LWA_INVOKE_MODE     = "response_stream"
      PORT                    = "8080"
    })
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_chat_stream,
    aws_iam_role_policy.lambda_inline,
  ]
}

resource "aws_lambda_function_url" "chat_stream" {
  count              = var.enable_chat_streaming ? 1 : 0
  function_name      = aws_lambda_function.chat_stream[0].function_name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM"

  cors {
    allow_credentials = false
    allow_origins     = var.cors_allowed_origins
    allow_methods     = ["POST"]
    allow_headers     = ["content-type", "authorization"]
    max_age           = 300
  }
}

resource "aws_lambda_permission" "chat_stream_url_public" {
  count                  = var.enable_chat_streaming ? 1 : 0
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.chat_stream[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# AWS now requires both InvokeFunctionUrl *and* InvokeFunction for Function URL
# access (AuthType NONE). Without this second statement the URL returns 403.
resource "aws_lambda_permission" "chat_stream_invoke_public" {
  count         = var.enable_chat_streaming ? 1 : 0
  statement_id  = "AllowPublicFunctionUrlInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.chat_stream[0].function_name
  principal     = "*"
}
