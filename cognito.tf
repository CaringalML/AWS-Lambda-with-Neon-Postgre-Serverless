resource "aws_cognito_user_pool" "main" {
  name                     = "${var.lambda_function_name}-users-${var.environment}"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Your verification code — Serverless Web App"
    email_message        = <<-HTML
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      </head>
      <body style="margin:0;padding:0;background-color:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f172a;padding:40px 16px;">
          <tr>
            <td align="center">
              <table width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">

                <!-- Header -->
                <tr>
                  <td align="center" style="padding-bottom:32px;">
                    <span style="color:#38bdf8;font-size:22px;font-weight:700;letter-spacing:0.5px;">
                      Serverless Web App
                    </span>
                  </td>
                </tr>

                <!-- Card -->
                <tr>
                  <td style="background-color:#1e293b;border:1px solid #334155;border-radius:16px;padding:40px 36px;">

                    <p style="margin:0 0 8px;color:#94a3b8;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:1px;">
                      Verification Code
                    </p>
                    <p style="margin:0 0 28px;color:#f1f5f9;font-size:17px;line-height:1.6;">
                      Use the code below to verify your email address. It expires in <strong style="color:#f1f5f9;">24 hours</strong>.
                    </p>

                    <!-- Code box -->
                    <div style="background-color:#0f172a;border:1px solid #38bdf8;border-radius:12px;padding:24px;text-align:center;margin-bottom:28px;">
                      <span style="font-size:42px;font-weight:700;letter-spacing:12px;color:#38bdf8;font-family:'Courier New',Courier,monospace;">
                        {####}
                      </span>
                    </div>

                    <p style="margin:0;color:#64748b;font-size:14px;line-height:1.6;">
                      If you did not create an account, you can safely ignore this email.
                      Someone may have entered your email address by mistake.
                    </p>

                  </td>
                </tr>

                <!-- Footer -->
                <tr>
                  <td align="center" style="padding-top:28px;">
                    <p style="margin:0;color:#475569;font-size:12px;">
                      Powered by Django &amp; AWS Lambda
                    </p>
                  </td>
                </tr>

              </table>
            </td>
          </tr>
        </table>
      </body>
      </html>
    HTML
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cognito_user_pool_client" "main" {
  name         = "${var.lambda_function_name}-client-${var.environment}"
  user_pool_id = aws_cognito_user_pool.main.id

  # No client secret — Lambda calls Cognito directly via IAM, no secret needed
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Token validity
  access_token_validity  = 1   # hours
  id_token_validity      = 1   # hours
  refresh_token_validity = 30  # days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

resource "aws_cognito_user" "admin" {
  user_pool_id = aws_cognito_user_pool.main.id
  username     = var.cognito_admin_email

  password = var.cognito_admin_password

  attributes = {
    email          = var.cognito_admin_email
    email_verified = "true"
  }

  message_action = "SUPPRESS"

  lifecycle {
    ignore_changes = [password]
  }
}
