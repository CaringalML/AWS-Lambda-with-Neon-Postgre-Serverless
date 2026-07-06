# ACM certificate — must be in the SAME region as API Gateway (us-east-1)
resource "aws_acm_certificate" "main" {
  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Environment = var.environment
  }
}

# Output the validation record so you can add it in Cloudflare
# After adding it, ACM validates and this resource completes
resource "aws_acm_certificate_validation" "main" {
  certificate_arn = aws_acm_certificate.main.arn

  # No validation_record_fqdns — Cloudflare handles DNS, not Route53.
  # Terraform will wait here (up to 75 min) until you add the CNAME in Cloudflare.
  timeouts {
    create = "75m"
  }
}

# API Gateway v2 custom domain
resource "aws_apigatewayv2_domain_name" "main" {
  domain_name = var.custom_domain

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.main.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = {
    Environment = var.environment
  }
}

# Map the API + stage to the custom domain root (no stage prefix in URL)
resource "aws_apigatewayv2_api_mapping" "main" {
  api_id      = aws_apigatewayv2_api.serverless_web_app.id
  domain_name = aws_apigatewayv2_domain_name.main.id
  stage       = aws_apigatewayv2_stage.serverless_web_app.id
}
