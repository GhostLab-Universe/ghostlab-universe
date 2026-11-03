# Mumbai restricted-eval inputs. No secrets here: keys live only in the
# operator-owned /etc/ghostlab/api.env on the host, which is never in Git.

variable "region" {
  description = "AWS region for the eval host."
  type        = string
  default     = "ap-south-1"
}

variable "project" {
  description = "Tag prefix for every resource."
  type        = string
  default     = "ghostlab-mumbai-eval"
}

variable "domain_name" {
  description = "Public DNS name for the eval UI/API (e.g. eval.example.org). Empty means IP-only mode: no Route53 records and the proxy binds localhost-only until a domain is set."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Existing public hosted-zone id for domain_name. Empty with a non-empty domain_name means Terraform creates the zone."
  type        = string
  default     = ""
}

variable "acme_email" {
  description = "Contact email for Let's Encrypt issuance. Required when domain_name is set; unused in IP-only mode."
  type        = string
  default     = ""
}

variable "admin_cidr" {
  description = "Single CIDR allowed SSH (port 22), e.g. 203.0.113.7/32. Empty (the default) means no SSH ingress rule at all: deny by default."
  type        = string
  default     = ""
}

variable "key_name" {
  description = "EC2 key-pair name for operator SSH. Empty means no key is attached."
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "EC2 size for the single eval host."
  type        = string
  default     = "t3.medium"
}

variable "root_volume_gb" {
  description = "gp3 root volume size in GiB. The SQLite storage root lives on it at /var/lib/ghostlab."
  type        = number
  default     = 30
}

variable "vpc_cidr" {
  description = "VPC CIDR for the eval network."
  type        = string
  default     = "10.20.0.0/16"
}

variable "subnet_cidr" {
  description = "Single public subnet CIDR (the only subnet in this deployment)."
  type        = string
  default     = "10.20.1.0/24"
}

variable "backup_bucket_prefix" {
  description = "Prefix for the versioned, encrypted backup bucket (a random suffix is added)."
  type        = string
  default     = "ghostlab-mumbai-backup"
}

variable "backup_retention_days" {
  description = "S3 lifecycle expiry for backup prefixes, in days."
  type        = number
  default     = 30
}
