output "public_ip" {
  description = "Elastic IP of the eval host (the IP-only address)."
  value       = aws_eip.eval.public_ip
}

output "address" {
  description = "What evaluators open: the domain when set, else the Elastic IP over fenced SSH forwarding."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "http://127.0.0.1:8080 via: ssh -L 8080:127.0.0.1:8080 ubuntu@${aws_eip.eval.public_ip}"
}

output "backup_bucket" {
  description = "Versioned, encrypted S3 bucket receiving the hourly SQLite backups."
  value       = aws_s3_bucket.backup.bucket
}

output "zone_id" {
  description = "Route53 zone id in use, or empty in IP-only mode."
  value       = local.zone_id
}

output "ssh" {
  description = "Operator SSH target (works only when admin_cidr and key_name are set)."
  value       = "ssh ubuntu@${aws_eip.eval.public_ip}"
}
