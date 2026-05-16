resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project_name}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = var.cors_allowed_origins
    allow_methods = ["GET", "POST", "PATCH", "DELETE", "OPTIONS", "PUT"]
    allow_headers = ["content-type", "authorization"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

locals {
  api_routes = [
    "GET /glossary",
    "POST /chat",
    "GET /roasters",
    "POST /roasters",
    "PATCH /roasters/{roasterId}",
    "GET /coffees",
    "POST /coffees",
    "PATCH /coffees/{coffeeId}",
    "DELETE /coffees/{coffeeId}",
    "GET /brews",
    "POST /brews",
    "PATCH /brews/{brewId}",
    "DELETE /brews/{brewId}",
    "GET /equipment",
    "POST /equipment",
    "PATCH /equipment/{equipId}",
    "GET /cafes",
    "POST /cafes",
    "PATCH /cafes/{cafeId}",
    "GET /visits",
    "POST /visits",
    "GET /profile",
    "PATCH /profile",
  ]
}

resource "aws_apigatewayv2_route" "routes" {
  for_each  = toset(local.api_routes)
  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"

  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
