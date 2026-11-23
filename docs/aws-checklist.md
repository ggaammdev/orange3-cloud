# AWS checklist

Manual steps to check the whole add-on on a real AWS account: setup, a machine, the demo workflow, the idle shutdown and the cleanup. Run them before a release, and after changes to the AWS provider, the machine image or the bootstrap.

Use an account meant for testing, with a budget alert and a vCPU quota of at least 4 in the region ([setup-aws.md](setup-aws.md)). The Small machine (`m7i.xlarge`, about $0.22 an hour in `us-east-1`) is enough, and one pass costs a few cents.

Steps 1–5 are also automated, without the windows. `TestRealAwsDemo` in `tests/test_demo.py` has two tests:

- `test_demo_on_aws` opens the workflow as Orange's canvas does, starts a Small machine from the Cloud settings dialog with a 3-minute idle timeout, waits for every widget, then waits for the machine to stop by itself while Orange is open, and checks that the instance powered itself off and that no instance, volume or security group is left (about 7 minutes);
- `test_stopped_while_orange_closed` starts a Small machine and closes the session, as when Orange quits, waits until the instance is `stopped` by itself, then opens a new session, which finds it stopped, deletes it and records its cost (running, then its volume while stopped; about 6 minutes).

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AWS_VM_TESTS=1 .venv/bin/pytest tests/test_demo.py -k RealAws -s
```

## Before you start

- Orange with the add-on from a source checkout (`scripts/dev-setup.sh`), and the AWS CLI (2.32 or newer) with a signed-in profile.
- A development version uses the private development image `:dev` from the Artifact Registry of the Google Cloud project chosen on the Google Cloud page, pulled with a short-lived token of `orange3-cloud-image-reader` that your Google sign-in makes (see "AWS machines" and "Development images" in [dev.md](dev.md)). Make sure the image of the commit you test has been pushed, or set `ORANGE3_CLOUD_AWS_IMAGE` to a `:dev-<sha>` tag.
- The demo data: `.venv/bin/python scripts/make-demo-data.py` writes `examples/demo-data.csv.gz` (1,000,000 rows, about 24 MB).
- Keep these commands at hand to see what is in the account (put your profile in `PROFILE` and the region in `REGION`):

  ```sh
  aws ec2 describe-instances --profile $PROFILE --region $REGION \
      --filters Name=tag:orange3-cloud,Values=true Name=instance-state-name,Values=pending,running,stopping,stopped \
      --query 'Reservations[].Instances[].[InstanceId,State.Name,StateReason.Code]'
  aws ec2 describe-volumes --profile $PROFILE --region $REGION \
      --filters Name=tag:orange3-cloud,Values=true --query 'Volumes[].[VolumeId,State]'
  aws ec2 describe-security-groups --profile $PROFILE --region $REGION \
      --filters Name=group-name,Values=orange3-cloud-https --query 'SecurityGroups[].[GroupId,IpPermissions]'
  ```

  All three should list nothing before you start.

## 1. Setup

1. `scripts/run-orange.sh examples/local-demo.ows`. The widgets say they aren't connected to a cloud machine.
2. Click "Cloud settings…" in any Cloud widget and choose **AWS**.
   - [ ] The page shows the AWS CLI's path, the profiles from `~/.aws/config`, and your user and account. Without a sign-in, "Sign in to AWS" opens AWS's sign-in page in the browser and the account appears afterwards. While it waits, "Cancel sign-in" stops it ("Sign-in cancelled.").
   - [ ] "Region" lists the regions enabled in the account. Choose the test region.
   - [ ] "Machine size" lists the sizes the region offers, each with an hourly price, and the prices' date. Choose **Small (testing)**.
   - [ ] The setup check passes (one line). With a size bigger than the quota allows, the quota check fails and says how many vCPUs to request, and Start is off.
3. Set "Stop when idle for" to **5 min**.

## 2. Start

1. Click **Start**.
   - [ ] The dialog shows the machine's progress: the security group, creating the machine, booting, installing Python and your packages, starting the server. AWS shows the console about a minute late, so "Booting the machine" may stay for most of the start.
   - [ ] It connects in about a minute. "Price" shows about $0.22/h, and "Stops in" counts down the idle time.
2. In the EC2 console or with the commands above:
   - [ ] one instance `orange3-cloud-<id>`, `m7i.xlarge`, tags `orange3-cloud=true` and `orange3-cloud-session=<id>`, shutdown behaviour "Stop", IMDSv2 required; its `gp3` volume has the same tags and is deleted on termination,
   - [ ] the security group `orange3-cloud-https` allows only TCP 8443 and only from your public IP address (`/32`).

## 3. The demo workflow

Cloud File starts as soon as the machine is connected, and the rest of the workflow follows by itself.

- [ ] **Cloud File:** the first time, it asks before creating the account's S3 bucket. It uploads the file to the bucket (with progress), and the machine copies it from there and loads 1,000,000 rows. Loading the workflow again doesn't upload the file again.
- [ ] **Cloud Test & Score:** 5-fold cross-validation of Random Forest and Logistic Regression on all rows. AUC about 0.83 and 0.77. The status line says how long it took and what it cost ("Cost about $… at $0.22/h.").
- [ ] **ROC Analysis** (Orange's own widget) draws both curves.
- [ ] **Cloud Learner** trains Logistic Regression on the machine, and **Cloud Predictions** predicts all rows.
- [ ] **Cloud Data Table** shows 1,000,000 rows with the predictions; paging works.
- [ ] No widget shows an error. "Cost so far" in the dialog has grown, with the jobs' share in brackets.

## 4. Idle shutdown

Leave the workflow alone. Don't click anything in the Cloud widgets.

- [ ] Within about a minute after the idle timeout has passed since the last job, the dialog says "The machine stopped: it was idle for longer than the idle timeout." and the widgets say they aren't connected.
- [ ] The instance first stopped by itself (in the console, its state reason is "Client.InstanceInitiatedShutdown"), then the add-on terminated it. The three commands above list nothing.

## 5. Closed Orange, and Stop

1. Start the machine again and wait until Cloud File has loaded.
2. Close Orange and open it again with the workflow.
   - [ ] It reconnects to the same machine, and the widgets work without starting a new one.
3. Close Orange and wait for the idle timeout and a minute more.
   - [ ] The instance is `stopped`, with the reason "Client.InstanceInitiatedShutdown", and has no public IP address. Its volume is still there.
4. Open Orange again.
   - [ ] The dialog says "The machine stopped." The three commands list nothing: Orange terminated the stopped instance, and its volume and the security group are gone.
5. Start a machine again, then click **Stop**.
   - [ ] The dialog says the machine is stopped within about a minute, and records its cost.
   - [ ] The three commands list nothing.

## 6. Clean up

- [ ] In Cloud File, "Uploaded files…" lists the demo file; delete it. The bucket stays, empty, for next time (an empty bucket costs nothing).
- [ ] The three commands list nothing.

## What to expect

Measured on a Small machine in `us-east-1`, with the demo's 1,000,000 rows, from a connection that reads `us-east-1` at 2–3 MB/s:

| Step | Time |
|---|---|
| Start to connected | about 1 min: the security group and the instance ~5 s, then booting, downloading the machine image and installing Python and the packages |
| Cloud File (upload and load) | under 30 s, most of it the upload (24 MB in about 9 s here) |
| Cloud Test & Score | about 1 min on the machine (both learners, 5 folds, all rows), plus 20–40 s to download the 64 MB of results |
| Cloud Learner and Cloud Predictions | about 1 s and 4 s |
| Whole workflow after connecting | about 2 min |
| Idle shutdown noticed (Orange open) | within a minute after the idle timeout |
| Instance `stopped` (Orange closed) | about half a minute after the idle timeout |
| Stop | under a minute |
| Cost of one pass | about $0.03 |
