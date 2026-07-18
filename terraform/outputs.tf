output "api_endpoint" {
  description = "Base URL for the HTTP API."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "table_name" {
  value = aws_dynamodb_table.main.name
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.lambda.name
}

output "chat_stream_url" {
  description = "Function URL for the streaming chat endpoint (POST /chat/stream). Set as window.DIALIN_CONFIG.streamApiBase in web/dialin-config.js, without the trailing slash. Null unless enable_chat_streaming = true."
  value       = var.enable_chat_streaming ? aws_lambda_function_url.chat_stream[0].function_url : null
}
