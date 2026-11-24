# Setting up AWS

The add-on can start machines in your own AWS account. This guide sets up the account once. It takes about 15 minutes, plus the wait if you need a higher vCPU quota. Orange checks the account for you and says what is still missing, so you can also start at step 4 and come back here when the check asks for something.

You need:

- an AWS account, with a payment method,
- Orange with the `orange3-cloud` add-on (see the [README](../README.md)).

## 1. An account and a user to sign in with

1. If you don't have an AWS account, create one at [aws.amazon.com](https://aws.amazon.com) (**Create an AWS account**). AWS asks for a payment method and verifies your phone number.
2. **Don't work as the account's root user** (the email address you signed up with). In the console, open **IAM → Users → Create user**, allow it to sign in to the console, and give it permissions:
   - in an account of your own, the `AdministratorAccess` policy is simplest,
   - otherwise, the add-on needs the policies `AmazonEC2FullAccess`, `AmazonS3FullAccess` and `AmazonSSMReadOnlyAccess`, plus `SignInLocalDevelopmentAccess` for signing in with the AWS CLI's browser sign-in (step 4).

   If your organization uses IAM Identity Center (single sign-on), you don't need an IAM user: ask for a permission set with the permissions above, and sign in through it.
3. Sign in to the console as that user. The add-on uses EC2 (the machine and a security group), S3 (a bucket for your files) and a public parameter of Systems Manager (the machine image's ID). It creates nothing else, and nothing outside the region you choose.

## 2. A budget alert

The add-on stops idle machines and shows what each job cost, but it has no spending limit of its own. A budget alert emails you when the month's costs reach an amount you choose:

1. In the console, open **Billing and Cost Management → Budgets → Create budget**.
2. Choose the **Monthly cost budget** template, an amount (for example $20) and the email addresses to alert. AWS alerts at 85% and 100% of the amount, and when the forecast exceeds it.

An alert doesn't stop anything: it only sends an email.

## 3. The AWS CLI

The add-on signs in to AWS with the AWS command-line tool, version 2.32 or newer.

1. Install it as [AWS's instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) describe. On a Mac with Homebrew, `brew install awscli` works too.
2. You don't need to run `aws configure`. Orange finds `aws` in the usual install folders and on your shell's `PATH`, even when Orange is started from the Dock.

## 4. Sign in from Orange

1. Open any Cloud widget, for example Cloud File, and click **Cloud settings…**.
2. Choose **AWS**.
3. **Profile** lists the AWS CLI's profiles (in `~/.aws/config`). Without any, it shows `default`, which signing in creates. To use a profile of its own name, create it in a terminal with `aws login --profile orange3-cloud` (any name works), then click **Reload**.
4. Click **Sign in to AWS**. Your browser opens AWS's sign-in page. Choose your console session or sign in, and allow access. Back in Orange, the dialog shows your user and account.

   This runs `aws login --profile <profile>`, which keeps short-lived credentials on your computer, where AWS's tools keep them, and renews them for up to 12 hours. After that, click **Sign in to AWS** again. For a profile of IAM Identity Center, it runs `aws sso login --profile <profile>` (set such a profile up with `aws configure sso`). A profile with access keys needs no sign-in.

5. Choose a **region** near you or near your data, for example `us-east-1`, `eu-central-1` or `ap-southeast-1`. The list has the regions enabled in your account. Prices differ between regions.

## 5. The setup check

Below the region, the dialog checks everything a machine needs and shows what is missing, with a link to the right page of the console. When everything passes, it shows one line, "The AWS account is ready".

| Check | What to do if it fails |
|---|---|
| Signed in to AWS | Click **Sign in to AWS** (step 4). |
| Region enabled | Some regions are off until you enable them in the account's settings (**Account → AWS Regions**). Or choose another region. |
| Default VPC in the region | Machines start in the region's default network. Accounts have one in every region unless someone deleted it: create it again in the VPC console (**Your VPCs → Actions → Create default VPC**). |
| Permissions for machines, security groups and storage | The check tries each action without doing it, and lists the ones you may not do. Ask the account's administrator for them (step 1). Stopping and deleting machines, reading their console and writing to S3 can't be tried that way, so the check asks IAM's policy simulator; if you may not use it, they are listed as not checked and the check still passes. |
| vCPU quota in the region | Your running machines may use only so many vCPUs in a region. Request more (see below), choose a smaller size, or stop other instances. |

**Requesting more quota:** all the add-on's sizes count against one quota per region, "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances" (`L-1216C47A`), measured in vCPUs. New accounts often have only 5 to 16. The check links to it in the console's **Service Quotas**: click **Request increase at account level** and ask for at least the number of vCPUs the check says. Small needs 4, Medium 16, Large 32. AWS reviews the request, which can take from minutes to a day or more; while one is open, the check says so.

## 6. Machine size and limits

**Machine size** lists the sizes your region offers, with their hourly list price there. Cloud File suggests a size that fits the file you load ("fits your data").

| Size | Instance type | vCPUs | Memory | Price in `us-east-1` |
|---|---|---:|---:|---:|
| Small (testing) | `m7i.xlarge` | 4 | 16 GiB | $0.22/h |
| Medium (default) | `m7i.4xlarge` | 16 | 64 GiB | $0.82/h |
| Medium, high memory | `r7i.4xlarge` | 16 | 128 GiB | $1.07/h |
| Large | `m7i.8xlarge` | 32 | 128 GiB | $1.63/h |
| Large, high memory | `r7i.8xlarge` | 32 | 256 GiB | $2.13/h |
| Extra large | `m7i.16xlarge` | 64 | 256 GiB | $3.24/h |
| Extra large, high memory | `r7i.16xlarge` | 64 | 512 GiB | $4.25/h |
| Huge | `m7i.24xlarge` | 96 | 384 GiB | $4.85/h |
| Huge, high memory | `r7i.24xlarge` | 96 | 768 GiB | $6.37/h |

The prices are AWS's on-demand list prices of November 2026 for Linux, with a 100 GB `gp3` boot volume and the machine's public IPv4 address included. Discounts, the free tier, network traffic and S3 storage aren't included.

As a rule of thumb, a table needs about 8 bytes per value in memory (rows × columns × 8), and learning needs several times that.

- **Boot disk:** 100 GB by default, from 50 to 2000 GB. The disk holds the files the machine loads (it copies files from S3 to its disk first), Python and your packages. Make it larger for big files.
- **Stop when idle for:** 30 minutes by default. The machine stops when no job has run and no Cloud widget has asked it for anything for this long. If it hasn't heard from Orange at all for this long, for example because Orange crashed or the laptop slept, it also cancels its jobs, which nobody is waiting for.
- **Stop after at most:** 12 hours by default, even while it works.

These apply to the next machine you start.

## 7. Start a machine

Click **Start**. The dialog shows the steps:

1. allowing your computer's IP address through the security group `orange3-cloud-https`,
2. creating the machine `orange3-cloud-<id>` (an EC2 instance),
3. booting, then installing Python, Orange and your packages at your exact versions,
4. starting the server.

It usually takes about a minute. AWS shows the machine's progress about a minute late, so the dialog may say "Booting the machine" for most of that time. Then the Cloud widgets say "Connected", and the dialog shows the machine's price, its cost so far and the time until it stops.

When you quit Orange while the machine runs, the Cloud widgets cancel their jobs, and Orange asks what to do with the machine: **Stop and quit** stops it first, **Keep it running** leaves it running until its idle time is over, and **Cancel** keeps Orange open. When you open Orange again while it runs, Orange connects to it again.

## 8. Your files in the S3 bucket

When Cloud File loads a file from your computer for the first time, it asks to create an S3 bucket in your account, `orange3-cloud-<random>`, in the machine's region. The bucket is private: all public access is blocked. Cloud File uploads the file there in parts, and an upload continues where it stopped if the connection breaks.

Files stay in the bucket, so loading the same file in a later session doesn't upload it again. S3 storage costs about $0.023 per GB a month in `us-east-1`. **Uploaded files…** in Cloud File lists them and deletes the ones you check. Deleting a file doesn't affect tables already loaded on a running machine.

The machine has no AWS access of its own. For each load, the add-on signs a short-lived link to the file with your sign-in, and the machine copies the file to its disk through it before reading it. So files in your own buckets (`s3://...`) load the same way, as long as your profile may read them. Files at `https://` URLs are loaded by the machine directly. Reading a bucket in the machine's region costs nothing; from another region, AWS charges for the transfer.

## 9. Stop, and check that nothing is left

- **Stop machine** in any Cloud widget, or **Stop** in Cloud settings, terminates the machine, deletes its volume, and deletes the security group.
- A machine also stops by itself after the idle time or the maximum lifetime. If Orange is open then, Orange deletes it. If Orange was closed, the instance is stopped: its compute and its IP address cost nothing any more, but its volume stays (100 GB costs about $8 a month) until you open Orange again, which deletes it.
- If Orange quits while a machine is starting, the machine may be left behind. Cloud settings lists such machines with a **Delete them…** button.

To check yourself, open **EC2 → Instances** in the console and look for machines named `orange3-cloud-…`, or run (with your profile and region):

```sh
aws ec2 describe-instances --profile PROFILE --region REGION \
    --filters Name=tag:orange3-cloud,Values=true --query 'Reservations[].Instances[].[InstanceId,State.Name]'
aws ec2 describe-volumes --profile PROFILE --region REGION \
    --filters Name=tag:orange3-cloud,Values=true --query 'Volumes[].VolumeId'
aws ec2 describe-security-groups --profile PROFILE --region REGION \
    --filters Name=group-name,Values=orange3-cloud-https --query 'SecurityGroups[].GroupId'
```

Terminated instances stay in the list for about an hour; only `running`, `stopping` and `stopped` ones cost money. All the add-on's instances, volumes, security groups and buckets have the tag `orange3-cloud=true`. To see their costs in Cost Explorer, activate that tag in **Billing and Cost Management → Cost allocation tags**.

## Troubleshooting

- **"AWS CLI: Not found"**: install it (step 3) and click **Reload**. If it is installed in an unusual place, start Orange from a terminal where `aws` works.
- **"Not signed in"** or a sign-in that stopped working: click **Sign in to AWS** (or **Sign in again**). A sign-in lasts up to 12 hours.
- **The sign-in fails at once with the AWS CLI's message**: `aws login` signs in only with console sign-ins. For a profile with access keys there is nothing to sign in to; for IAM Identity Center, set the profile up with `aws configure sso`. An AWS CLI older than 2.32 doesn't know `aws login`: update it.
- **A quota check fails**: choose a smaller size or another region, or request more quota (step 5).
- **The machine doesn't connect**: your IP address may have changed, or a VPN or company network may block port 8443. The add-on updates the security group when your address changes; try again after leaving the VPN. The dialog's messages show where the start stopped.
- **"Not enough memory"** or a warning in Cloud File: choose a larger size. Cloud File's estimate suggests one.
- **A file in S3 doesn't load**: Cloud File says whether it doesn't exist or your profile may not read it. The machine needs room on its disk for a copy of the file; make the boot disk larger for very large files.
- **A package failed to install on the machine**: Cloud settings lists the versions on your computer and on the machine, and the packages that couldn't be installed. Packages installed from a folder on your computer can't be copied.

## Remove everything

To remove everything the add-on created in your account:

1. Click **Stop** in Cloud settings, and **Delete them…** if it lists machines left behind.
2. Delete your uploaded files with **Uploaded files…** in Cloud File. To delete the bucket `orange3-cloud-…` too, empty it and delete it in the console's **S3 → Buckets**.
3. Delete the security group `orange3-cloud-https` if it's still there (**EC2 → Security Groups**, in each region you used).
