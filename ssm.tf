resource "aws_ssm_parameter" "database_url" {
  name        = "/${var.lambda_function_name}/${var.environment}/database-url"
  description = "Neon Postgres connection string for ${var.lambda_function_name}-${var.environment}"
  type        = "SecureString"
  value       = var.database_url

  tags = {
    Environment = var.environment
  }
}

resource "aws_ssm_parameter" "resend_api_key" {
  name        = "/${var.lambda_function_name}/${var.environment}/resend-api-key"
  description = "Resend API key for email notifications (${var.lambda_function_name}-${var.environment})"
  type        = "SecureString"
  value       = var.resend_api_key

  tags = {
    Environment = var.environment
  }
}

resource "aws_ssm_parameter" "admin_password" {
  name        = "/${var.lambda_function_name}/${var.environment}/admin-password"
  description = "Admin login password for ${var.lambda_function_name}-${var.environment}"
  type        = "SecureString"
  value       = var.cognito_admin_password

  # Never overwrite on redeploy — password is managed via the UI (forgot password).
  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Environment = var.environment
  }
}
