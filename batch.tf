# ── Default VPC / subnets (used by Batch Fargate) ─────────────────────────
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ── Security group for Batch containers ────────────────────────────────────
resource "aws_security_group" "batch" {
  name        = "${var.lambda_function_name}-batch-${var.environment}"
  description = "Outbound-only SG for NovaDrive Batch workers"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Environment = var.environment }
}

# ── IAM: execution role (ECS task plumbing — pull image, push logs) ────────
resource "aws_iam_role" "batch_execution" {
  name = "${var.lambda_function_name}-batch-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = { Environment = var.environment }
}

resource "aws_iam_role_policy_attachment" "batch_execution_policy" {
  role       = aws_iam_role.batch_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── IAM: job role (what the container can do) ──────────────────────────────
resource "aws_iam_role" "batch_job" {
  name = "${var.lambda_function_name}-batch-job-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = { Environment = var.environment }
}

resource "aws_iam_role_policy" "batch_job_s3" {
  name = "${var.lambda_function_name}-batch-job-s3-${var.environment}"
  role = aws_iam_role.batch_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
        Resource = [
          aws_s3_bucket.drive.arn,
          "${aws_s3_bucket.drive.arn}/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "batch_job_dynamodb" {
  name = "${var.lambda_function_name}-batch-job-dynamodb-${var.environment}"
  role = aws_iam_role.batch_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
        ]
        Resource = [
          aws_dynamodb_table.folders.arn,
          "${aws_dynamodb_table.folders.arn}/index/*",
          aws_dynamodb_table.files.arn,
          "${aws_dynamodb_table.files.arn}/index/*",
          aws_dynamodb_table.batch_jobs.arn,
        ]
      }
    ]
  })
}

# ── Batch compute environment (Fargate Spot) ───────────────────────────────
resource "aws_batch_compute_environment" "novadrive" {
  compute_environment_name = "${var.lambda_function_name}-${var.environment}"
  type                     = "MANAGED"
  state                    = "ENABLED"

  compute_resources {
    type               = "FARGATE_SPOT"
    max_vcpus          = 4

    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.batch.id]
  }

  tags = { Environment = var.environment }
}

# ── Job queue ──────────────────────────────────────────────────────────────
resource "aws_batch_job_queue" "novadrive" {
  name     = "${var.lambda_function_name}-${var.environment}"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.novadrive.arn
  }

  tags = { Environment = var.environment }
}

# ── Job definition (Docker Hub image) ─────────────────────────────────────
resource "aws_batch_job_definition" "zip_folder" {
  name = "${var.lambda_function_name}-zip-folder-${var.environment}"
  type = "container"

  platform_capabilities = ["FARGATE"]

  container_properties = jsonencode({
    image            = "rencecaringal000/novadrive-batch:latest"
    jobRoleArn       = aws_iam_role.batch_job.arn
    executionRoleArn = aws_iam_role.batch_execution.arn

    # Overridden per-job for small zips; this is a safe default
    resourceRequirements = [
      { type = "VCPU",   value = "1" },
      { type = "MEMORY", value = "2048" },
    ]

    networkConfiguration = {
      assignPublicIp = "ENABLED"
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/aws/batch/${var.lambda_function_name}-${var.environment}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "zip-folder"
      }
    }

    # Default env — overridden at submit time for sensitive values
    environment = []
  })

  tags = { Environment = var.environment }
}

# ── CloudWatch log group for Batch ────────────────────────────────────────
resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.lambda_function_name}-${var.environment}"
  retention_in_days = 7

  tags = { Environment = var.environment }
}

# ── Outputs ───────────────────────────────────────────────────────────────
output "batch_job_queue" {
  value = aws_batch_job_queue.novadrive.name
}

output "batch_job_definition" {
  value = aws_batch_job_definition.zip_folder.name
}
