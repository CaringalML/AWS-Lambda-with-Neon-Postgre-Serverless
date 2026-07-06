resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.lambda_function_name}-${var.environment}"
  retention_in_days = 14

  tags = {
    Environment = var.environment
  }
}

# Alert when 429 throttling or other 4xx errors spike —
# fires if more than 50 4xx responses occur within a single 1-minute window.
resource "aws_cloudwatch_metric_alarm" "api_4xx_errors" {
  alarm_name          = "${var.lambda_function_name}-4xx-errors-${var.environment}"
  alarm_description   = "API Gateway 4XX errors exceeded 50 in 1 minute (includes 429 throttling)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "4XXError"
  namespace           = "AWS/ApiGateway"
  period              = 60
  statistic           = "Sum"
  threshold           = 50
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    ApiId = aws_apigatewayv2_api.serverless_web_app.id
    Stage = aws_apigatewayv2_stage.serverless_web_app.name
  }
}

# Alert on sudden request volume spike — a strong DDoS signal.
# Fires if more than 500 total requests arrive within a single 1-minute window.
resource "aws_cloudwatch_metric_alarm" "api_request_spike" {
  alarm_name          = "${var.lambda_function_name}-request-spike-${var.environment}"
  alarm_description   = "API Gateway request count exceeded 500 in 1 minute — possible DDoS"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Count"
  namespace           = "AWS/ApiGateway"
  period              = 60
  statistic           = "Sum"
  threshold           = 500
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    ApiId = aws_apigatewayv2_api.serverless_web_app.id
    Stage = aws_apigatewayv2_stage.serverless_web_app.name
  }
}
