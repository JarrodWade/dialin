locals {
  _lambda_bundle_trigger = sha256(join("", concat(
    [filesha256("${path.module}/../lambda/requirements.txt")],
    [for p in sort(fileset("${path.module}/../lambda", "*.py")) : filesha256("${path.module}/../lambda/${p}")],
  )))
}

resource "null_resource" "lambda_bundle" {
  triggers = {
    hashes = local._lambda_bundle_trigger
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
set -e
ROOT="${path.module}/../lambda"
ROOT="$(cd "$ROOT" && pwd)"
rm -rf "$ROOT/build"
mkdir -p "$ROOT/build"
python3 -m pip install -q -r "$ROOT/requirements.txt" -t "$ROOT/build" \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all:
rm -rf "$ROOT/build/youtube_transcript_api/test"
cp "$ROOT"/*.py "$ROOT/build/"
EOT
  }
}

data "archive_file" "lambda_zip" {
  depends_on = [null_resource.lambda_bundle]

  type        = "zip"
  source_dir  = "${path.module}/../lambda/build"
  output_path = "${path.module}/build/lambda.zip"
  excludes = [
    "__pycache__",
    "*.pyc",
    "**/youtube_transcript_api/test/**",
  ]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project_name}-api"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30
  memory_size      = 512

  environment {
    variables = {
      TABLE_NAME                       = aws_dynamodb_table.main.name
      BEDROCK_MODEL_ID                 = var.bedrock_model_id
      BEDROCK_REGION                   = var.region
      BEDROCK_EMBEDDING_MODEL_ID       = trimspace(var.bedrock_embedding_model_id)
      MAX_OUTPUT_TOKENS                = tostring(var.max_output_tokens)
      MAX_TOOL_ITERATIONS              = tostring(var.max_tool_iterations)
      TAVILY_API_KEY                   = var.tavily_api_key
      WEBSEARCH_CACHE_TTL_SECONDS      = tostring(var.websearch_cache_ttl_seconds)
      WEBSEARCH_MONTHLY_LIMIT_PER_USER = tostring(var.websearch_monthly_limit_per_user)
      ALLOW_CLIENT_USER_ID             = local.clerk_auth_enabled ? "false" : "true"
      CLERK_JWT_ISSUER                 = trimspace(var.clerk_jwt_issuer)
      CLERK_ALLOWED_ORIGINS            = join(",", [for o in var.cors_allowed_origins : o if o != "*"])
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda_inline,
  ]
}
