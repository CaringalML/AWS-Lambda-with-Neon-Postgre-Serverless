variable "aws_region" {
  description = "AWS region where resources will be created"
  type        = string
  default     = "us-east-1"
}

variable "lambda_function_name" {
  description = "Base name for the Lambda function"
  type        = string
  default     = "serverless-web-app"
}

variable "environment" {
  description = "Deployment environment (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "database_url" {
  description = "Neon Postgres connection string (set via TF_VAR_database_url in GitHub secrets)"
  type        = string
  sensitive   = true
}

variable "custom_domain" {
  description = "Custom domain for the app (e.g. drive.nodepulsecaringal.xyz)"
  type        = string
  default     = "drive.nodepulsecaringal.xyz"
}

variable "resend_api_key" {
  description = "Resend API key for sending email notifications"
  type        = string
  sensitive   = true
}

variable "cognito_admin_email" {
  description = "Email address for the single admin account"
  type        = string
  default     = "lawrencecaringal5@gmail.com"
}

variable "cognito_admin_password" {
  description = "Password for the single admin account"
  type        = string
  sensitive   = true
}
