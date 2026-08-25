resource "aws_iam_role" "lambda_role" {
  name = "${var.lambda_function_name}-${var.environment}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Allow Lambda to decrypt SSM SecureString parameters
resource "aws_iam_role_policy" "lambda_ssm" {
  name = "${var.lambda_function_name}-${var.environment}-ssm-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
      }
    ]
  })
}

# Allow Lambda to read/write files in the drive S3 bucket
resource "aws_iam_role_policy" "lambda_s3_drive" {
  name = "${var.lambda_function_name}-${var.environment}-s3-drive-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:CopyObject",
          "s3:HeadObject",
          "s3:ListBucket",
          "s3:RestoreObject",
          # Multipart uploads for large files. PutObject already covers
          # CreateMultipartUpload/UploadPart/CompleteMultipartUpload; these
          # cover cancelling and inspecting an upload in progress.
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
          "s3:ListBucketMultipartUploads",
        ]
        Resource = [
          aws_s3_bucket.drive.arn,
          "${aws_s3_bucket.drive.arn}/*",
        ]
      }
    ]
  })
}

# Allow Lambda to read the admin password from SSM
resource "aws_iam_role_policy" "lambda_ssm_admin_password" {
  name = "${var.lambda_function_name}-${var.environment}-ssm-admin-password-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = aws_ssm_parameter.admin_password.arn
      }
    ]
  })
}

# Allow Lambda to read the CloudFront signing private key from SSM
resource "aws_iam_role_policy" "lambda_ssm_cloudfront_key" {
  name = "${var.lambda_function_name}-${var.environment}-ssm-cf-key-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.cloudfront_private_key.arn
      }
    ]
  })
}

# Allow Lambda to read the Resend API key from SSM
resource "aws_iam_role_policy" "lambda_ssm_resend_key" {
  name = "${var.lambda_function_name}-${var.environment}-ssm-resend-key-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.resend_api_key.arn
      }
    ]
  })
}

# Allow the web app Lambda to async-invoke the thumbnailer (backfill for
# files uploaded before thumbnails existed)
resource "aws_iam_role_policy" "lambda_invoke_thumbnailer" {
  name = "${var.lambda_function_name}-${var.environment}-invoke-thumbnailer-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.thumbnailer.arn
      }
    ]
  })
}

# Allow Lambda to submit and describe AWS Batch jobs
resource "aws_iam_role_policy" "lambda_batch" {
  name = "${var.lambda_function_name}-${var.environment}-batch-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "batch:SubmitJob",
          "batch:DescribeJobs",
        ]
        Resource = "*"
      }
    ]
  })
}

# Allow Lambda to call Cognito on behalf of the application
resource "aws_iam_role_policy" "lambda_cognito" {
  name = "${var.lambda_function_name}-${var.environment}-cognito-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cognito-idp:SignUp",
          "cognito-idp:ConfirmSignUp",
          "cognito-idp:InitiateAuth",
          "cognito-idp:GetUser",
          "cognito-idp:GlobalSignOut",
        ]
        Resource = aws_cognito_user_pool.main.arn
      }
    ]
  })
}
