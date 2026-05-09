data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/serverless_web_app"
  output_path = "${path.module}/lambda/serverless_web_app.zip"
}

resource "aws_lambda_function" "serverless_web_app" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.lambda_function_name}-${var.environment}"
  role             = aws_iam_role.lambda_role.arn
  handler          = "wsgi.handler"
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30

  depends_on = [aws_cloudwatch_log_group.lambda]

  environment {
    variables = {
      ENVIRONMENT            = var.environment
      DJANGO_SETTINGS_MODULE = "config.settings.prod"
      # DB credentials fetched from SSM at runtime — not stored as plain text here
      SSM_DATABASE_URL_NAME  = aws_ssm_parameter.database_url.name
      # Single-admin auth
      ADMIN_EMAIL    = var.cognito_admin_email
      ADMIN_PASSWORD = var.cognito_admin_password
      # NovaDrive
      DRIVE_BUCKET_NAME               = aws_s3_bucket.drive.bucket
      CLOUDFRONT_DOMAIN               = aws_cloudfront_distribution.drive.domain_name
      CLOUDFRONT_KEY_PAIR_ID          = aws_cloudfront_public_key.drive.id
      CLOUDFRONT_PRIVATE_KEY_SSM_NAME = aws_ssm_parameter.cloudfront_private_key.name
      ALLOWED_HOSTS                   = var.custom_domain
      CSRF_TRUSTED_ORIGINS            = "https://${var.custom_domain}"
      SSM_RESEND_API_KEY_NAME         = aws_ssm_parameter.resend_api_key.name
      DRIVE_FROM_EMAIL                = "drive@nodepulsecaringal.xyz"
      BATCH_JOB_QUEUE                 = aws_batch_job_queue.novadrive.name
      BATCH_JOB_DEFINITION            = aws_batch_job_definition.zip_folder.name
    }
  }

  tags = {
    Environment = var.environment
  }
}

# ── Glacier restore-completed notification Lambda ──────────────────────────
resource "aws_cloudwatch_log_group" "notify_lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}-notify-${var.environment}"
  retention_in_days = 7
}

resource "aws_lambda_function" "notify" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.lambda_function_name}-notify-${var.environment}"
  role             = aws_iam_role.lambda_role.arn
  handler          = "notify.handler"
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60

  depends_on = [aws_cloudwatch_log_group.notify_lambda]

  environment {
    variables = {
      AWS_REGION_NAME         = var.aws_region
      SSM_DATABASE_URL_NAME   = aws_ssm_parameter.database_url.name
      SSM_RESEND_API_KEY_NAME = aws_ssm_parameter.resend_api_key.name
      DRIVE_FROM_EMAIL        = "noreply@nodepulsecaringal.xyz"
      DRIVE_URL               = "https://${var.custom_domain}/drive/"
    }
  }

  tags = {
    Environment = var.environment
  }
}

# Allow S3 to invoke the notify Lambda
resource "aws_lambda_permission" "s3_invoke_notify" {
  statement_id  = "AllowS3InvokeNotify"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.drive.arn
}
