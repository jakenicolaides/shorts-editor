#!/bin/sh
# One-time AWS setup for the shorts editor. Run from your own terminal with an
# admin profile:   AWS_PROFILE=shorts-admin bin/aws-setup.sh
#
# Creates:  bucket twixtle-shorts (eu-west-1, private, 14-day expiry)
#           IAM user shorts-uploader with access to that bucket only
#           writes the uploader's keys into .env (never printed)
set -eu
cd "$(dirname "$0")/.."

BUCKET=${BUCKET:-twixtle-shorts}
REGION=${REGION:-eu-west-1}
USER=${USER_NAME:-shorts-uploader}

echo "== identity"
aws sts get-caller-identity --output text

echo "== bucket $BUCKET"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{
  "Rules": [{"ID": "expire-shorts", "Status": "Enabled", "Filter": {"Prefix": "shorts/"},
             "Expiration": {"Days": 14},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}'
aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration '{
  "CORSRules": [{"AllowedOrigins": ["https://twixtle.games"], "AllowedMethods": ["GET", "HEAD"],
                 "AllowedHeaders": ["*"], "MaxAgeSeconds": 3600}]}'

echo "== user $USER"
if ! aws iam get-user --user-name "$USER" >/dev/null 2>&1; then
  aws iam create-user --user-name "$USER" >/dev/null
fi
aws iam put-user-policy --user-name "$USER" --policy-name "$BUCKET-rw" --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:ListBucket\"], \"Resource\": \"arn:aws:s3:::$BUCKET\"},
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\", \"s3:DeleteObject\"],
     \"Resource\": \"arn:aws:s3:::$BUCKET/*\"}
  ]}"

echo "== access key"
# drop any old keys so the user only ever has the one in .env
for k in $(aws iam list-access-keys --user-name "$USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name "$USER" --access-key-id "$k"
done
KEY_JSON=$(aws iam create-access-key --user-name "$USER" --output json)
AKID=$(printf '%s' "$KEY_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')
SECRET=$(printf '%s' "$KEY_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')

[ -f .env ] || cp .env.example .env
python3 - "$AKID" "$SECRET" "$BUCKET" "$REGION" <<'EOF'
import sys, re, pathlib
akid, secret, bucket, region = sys.argv[1:5]
p = pathlib.Path(".env"); s = p.read_text()
for k, v in (("AWS_ACCESS_KEY_ID", akid), ("AWS_SECRET_ACCESS_KEY", secret),
             ("S3_BUCKET", bucket), ("S3_REGION", region)):
    if re.search(rf"^{k}=", s, re.M):
        s = re.sub(rf"^{k}=.*$", f"{k}={v}", s, flags=re.M)
    else:
        s += f"\n{k}={v}"
p.write_text(s)
EOF
chmod 600 .env
echo "== done: uploader keys written to .env (key id $AKID)"
echo "Now delete the temporary admin user in the IAM console."
