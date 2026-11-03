# Mumbai restricted-eval network + host. Single VPC, single public subnet,
# one t3.medium, one Elastic IP, one private backup bucket. No database or
# worker ports are reachable: the API binds 127.0.0.1 behind Caddy on the host.

provider "aws" {
  region = var.region
}

locals {
  has_domain = var.domain_name != ""
  has_zone   = var.route53_zone_id != ""
  name       = var.project
}

# --- Network --------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.subnet_cidr
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "host" {
  name        = "${local.name}-host"
  description = "Eval host: HTTPS in, HTTP in for ACME only, fenced SSH."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from evaluators"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP for ACME http-01 only (Caddy redirects the rest)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH exists only when the operator supplies an admin CIDR. The default
  # (empty) creates no rule at all: deny by default, not 0.0.0.0/0.
  dynamic "ingress" {
    for_each = var.admin_cidr != "" ? [var.admin_cidr] : []
    content {
      description = "Operator SSH, fenced to one CIDR"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    description = "Outbound for package mirrors, ACME, S3 and the live model path"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-host" }
}

# --- Host -----------------------------------------------------------------

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "eval" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.host.id]
  key_name               = var.key_name != "" ? var.key_name : null

  iam_instance_profile = aws_iam_instance_profile.eval.name

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    tags = { Name = "${local.name}-root" }
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  user_data = templatefile("${path.module}/files/user-data.sh.tpl", {
    aws_region     = var.region
    backup_bucket  = aws_s3_bucket.backup.bucket
    domain_name    = var.domain_name
    acme_email     = var.acme_email
    project        = var.project
    debian_package = "ghostlab-eval"
  })

  tags = { Name = "${local.name}-host" }
}

resource "aws_eip" "eval" {
  instance = aws_instance.eval.id
  domain   = "vpc"

  tags = { Name = "${local.name}-eip" }
}

# --- Backup bucket --------------------------------------------------------

resource "random_id" "bucket" {
  byte_length = 4
}

resource "aws_s3_bucket" "backup" {
  bucket        = "${var.backup_bucket_prefix}-${random_id.bucket.hex}"
  force_destroy = false

  tags = { Name = "${local.name}-backup" }
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    id     = "expire-old-backups"
    status = "Enabled"

    filter {}

    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.backup_retention_days
    }
  }
}

# Least privilege for the hourly backup timer: the host can only touch the
# backup bucket, and only under way of the backup/restore scripts.
resource "aws_iam_role" "eval" {
  name = "${local.name}-host"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name}-host" }
}

resource "aws_iam_role_policy" "backup" {
  name = "${local.name}-backup"
  role = aws_iam_role.eval.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket",
      ]
      Resource = [
        aws_s3_bucket.backup.arn,
        "${aws_s3_bucket.backup.arn}/*",
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "eval" {
  name = "${local.name}-host"
  role = aws_iam_role.eval.name
}

# --- DNS (domain mode only) -----------------------------------------------
# Empty domain_name means IP-only mode: no zone, no records. The proxy then
# binds localhost-only (see deploy/caddy/Caddyfile.localhost) and the operator
# reaches the box over fenced SSH port-forwarding until a domain is set.

resource "aws_route53_zone" "eval" {
  count = local.has_domain && !local.has_zone ? 1 : 0

  name = var.domain_name

  tags = { Name = "${local.name}-zone" }
}

locals {
  zone_id = local.has_domain ? (local.has_zone ? var.route53_zone_id : aws_route53_zone.eval[0].zone_id) : ""
}

resource "aws_route53_record" "eval" {
  count = local.has_domain ? 1 : 0

  zone_id = local.zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.eval.public_ip]
}
