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

  # State bucket and lock table are pre-created manually (not managed by this Terraform config).
  backend "s3" {
    bucket         = "nova-drive-terraform-state"
    key            = "terraform/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "novadrive-terraform-lock"
    encrypt        = true
  }
}
