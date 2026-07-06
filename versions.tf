terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # State is stored in a dedicated bucket that is NOT managed by this Terraform config.
  # The bucket and DynamoDB table are bootstrapped by the deploy workflow before terraform init.
  backend "s3" {
    bucket         = "serverless-web-app-drive-dev"
    key            = "terraform/terraform.tfstate"
    region         = "ap-southeast-2"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}
