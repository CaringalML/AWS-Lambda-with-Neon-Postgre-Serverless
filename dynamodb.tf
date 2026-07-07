resource "aws_dynamodb_table" "folders" {
  name         = "${var.lambda_function_name}-folders-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "folder_id"

  attribute {
    name = "folder_id"
    type = "S"
  }
  attribute {
    name = "parent_id"
    type = "S"
  }
  attribute {
    name = "owner_sub"
    type = "S"
  }
  attribute {
    name = "name"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  # List subfolders of a given parent, sorted by name
  global_secondary_index {
    name            = "parent-index"
    hash_key        = "parent_id"
    range_key       = "name"
    projection_type = "ALL"
  }

  # List all folders for an owner, sorted by creation time
  global_secondary_index {
    name            = "owner-created-index"
    hash_key        = "owner_sub"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "files" {
  name         = "${var.lambda_function_name}-files-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "file_id"

  attribute {
    name = "file_id"
    type = "S"
  }
  attribute {
    name = "folder_id"
    type = "S"
  }
  attribute {
    name = "owner_sub"
    type = "S"
  }
  attribute {
    name = "uploaded_at"
    type = "S"
  }
  attribute {
    name = "s3_key"
    type = "S"
  }

  # List files in a folder, sorted by upload time (newest first via reverse scan)
  global_secondary_index {
    name            = "folder-index"
    hash_key        = "folder_id"
    range_key       = "uploaded_at"
    projection_type = "ALL"
  }

  # List all files for an owner — used for recycle bin, archive view, timeline
  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner_sub"
    range_key       = "uploaded_at"
    projection_type = "ALL"
  }

  # Unique s3_key lookup — used on upload to detect duplicates
  global_secondary_index {
    name            = "s3key-index"
    hash_key        = "s3_key"
    projection_type = "ALL"
  }

  tags = {
    Environment = var.environment
  }
}

# TTL auto-deletes expired batch job records (24h after completion)
resource "aws_dynamodb_table" "batch_jobs" {
  name         = "${var.lambda_function_name}-batch-jobs-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  attribute {
    name = "job_id"
    type = "S"
  }
  attribute {
    name = "owner_sub"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  # List batch jobs for an owner, sorted by creation time
  global_secondary_index {
    name            = "owner-created-index"
    hash_key        = "owner_sub"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "lambda_dynamodb" {
  name = "${var.lambda_function_name}-${var.environment}-dynamodb-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchWriteItem",
          "dynamodb:BatchGetItem",
        ]
        Resource = [
          aws_dynamodb_table.folders.arn,
          "${aws_dynamodb_table.folders.arn}/index/*",
          aws_dynamodb_table.files.arn,
          "${aws_dynamodb_table.files.arn}/index/*",
          aws_dynamodb_table.batch_jobs.arn,
          "${aws_dynamodb_table.batch_jobs.arn}/index/*",
        ]
      }
    ]
  })
}
