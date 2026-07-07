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
      # Single-admin auth
      ADMIN_EMAIL              = var.cognito_admin_email
      SSM_ADMIN_PASSWORD_NAME  = aws_ssm_parameter.admin_password.name
      # NovaDrive
      DRIVE_BUCKET_NAME               = aws_s3_bucket.drive.bucket
      CLOUDFRONT_DOMAIN               = aws_cloudfront_distribution.drive.domain_name
      CLOUDFRONT_KEY_PAIR_ID          = aws_cloudfront_public_key.drive.id
      CLOUDFRONT_PRIVATE_KEY_SSM_NAME = aws_ssm_parameter.cloudfront_private_key.name
      ALLOWED_HOSTS                   = var.custom_domain
      CSRF_TRUSTED_ORIGINS            = "https://${var.custom_domain}"
      COGNITO_CLIENT_ID               = aws_cognito_user_pool_client.main.id
      SSM_RESEND_API_KEY_NAME         = aws_ssm_parameter.resend_api_key.name
      DRIVE_FROM_EMAIL                = "drive@nodepulsecaringal.xyz"
      BATCH_JOB_QUEUE                 = aws_batch_job_queue.novadrive.name
      BATCH_JOB_DEFINITION            = aws_batch_job_definition.zip_folder.name
      # DynamoDB tables
      DYNAMODB_FOLDERS_TABLE          = aws_dynamodb_table.folders.name
      DYNAMODB_FILES_TABLE            = aws_dynamodb_table.files.name
      DYNAMODB_BATCH_JOBS_TABLE       = aws_dynamodb_table.batch_jobs.name
      # Thumbnail backfill (async invoke when a thumb is missing)
      THUMBNAILER_FUNCTION            = aws_lambda_function.thumbnailer.function_name
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
      DYNAMODB_FILES_TABLE    = aws_dynamodb_table.files.name
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

# ── Thumbnail generator Lambda ──────────────────────────────────────────────
# Fired on every upload; writes a small WebP to thumbs/{key}.webp so the grid
# never downloads full-size originals (or pays Glacier IR retrieval fees).
resource "aws_cloudwatch_log_group" "thumbnailer_lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}-thumbnailer-${var.environment}"
  retention_in_days = 7
}

resource "aws_lambda_function" "thumbnailer" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.lambda_function_name}-thumbnailer-${var.environment}"
  role             = aws_iam_role.lambda_role.arn
  handler          = "thumbnailer.handler"
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60
  memory_size      = 1024 # Pillow decode/resize of large photos

  depends_on = [aws_cloudwatch_log_group.thumbnailer_lambda]

  tags = {
    Environment = var.environment
  }
}

resource "aws_lambda_permission" "s3_invoke_thumbnailer" {
  statement_id  = "AllowS3InvokeThumbnailer"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.thumbnailer.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.drive.arn
}
