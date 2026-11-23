# Google Cloud checklist

Manual steps to check the whole add-on on a real Google Cloud project: setup, a machine, the demo workflow, the idle shutdown and the cleanup. Run them before a release, and after changes to the Google Cloud provider, the machine image or the bootstrap.

Use a project meant for testing, with billing enabled. The Small machine (`e2-standard-4`, about $0.15 an hour in `us-central1`) is enough, and one pass costs a few cents.

Steps 1–4 are also automated, without the windows: `TestRealDemo` in `tests/test_demo.py` opens the workflow as Orange's canvas does, starts a Small machine from the Cloud settings dialog with a 3-minute idle timeout, waits for every widget, then waits for the machine to stop by itself and checks that no VM, disk or firewall rule is left. It takes about 9 minutes:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_GCP_VM_TESTS=1 .venv/bin/pytest tests/test_demo.py -k test_demo_on_gcp -s
```

## Before you start

- Orange with the add-on from a source checkout (`scripts/dev-setup.sh`), and the `gcloud` tool.
- A development version uses the private development image `:dev` in the project's Artifact Registry (see "Development images" in [dev.md](dev.md)). Make sure the image of the commit you test has been pushed, or set `ORANGE3_CLOUD_GCP_IMAGE` to a `:dev-<sha>` tag.
- The demo data: `.venv/bin/python scripts/make-demo-data.py` writes `examples/demo-data.csv.gz` (1,000,000 rows, about 24 MB).
- Keep these commands at hand to see what is in the project (put your project in `PROJECT`):

  ```sh
  gcloud compute instances list --project=$PROJECT --filter=labels.orange3-cloud=true
  gcloud compute disks list --project=$PROJECT --filter=labels.orange3-cloud=true
  gcloud compute firewall-rules list --project=$PROJECT --filter=name=orange3-cloud-https
  ```

  All three should list nothing before you start.

## 1. Setup

1. `scripts/run-orange.sh examples/local-demo.ows`. The widgets say they aren't connected to a cloud machine.
2. Click "Cloud settings…" in any Cloud widget and choose **Google Cloud**.
   - [ ] The page shows the `gcloud` tool and your account. Without a sign-in, "Sign in with Google" opens Google's page in the browser and the account appears afterwards. While it waits, "Cancel sign-in" stops it ("Sign-in cancelled.").
   - [ ] Project, region and zone list your projects and the project's zones. Choose the test project.
   - [ ] "Machine size" lists the sizes the zone offers, each with an hourly price. Choose **Small (testing)**.
   - [ ] The setup check passes (one line). On a project that isn't ready, it lists exactly what's missing, with links.
3. Set "Stop when idle for" to **5 min**.

## 2. Start

1. Click **Start**.
   - [ ] The dialog shows the machine's progress: creating the firewall rule, creating the machine, downloading the machine image, installing Python and your packages, starting the server.
   - [ ] It connects in about one and a half minutes. "Price" shows about $0.15/h, and "Stops in" counts down the idle time.
2. In the Google Cloud console or with `gcloud`:
   - [ ] one VM `orange3-cloud-<id>`, `e2-standard-4`, labels `orange3-cloud=true` and `orange3-cloud-session=<id>`, network tag `orange3-cloud`,
   - [ ] the firewall rule `orange3-cloud-https` allows only TCP 8443 and only from your public IP address (`/32`).

## 3. The demo workflow

Cloud File starts as soon as the machine is connected, and the rest of the workflow follows by itself.

- [ ] **Cloud File:** the first time, it asks before creating the project's storage bucket. It uploads the file to the bucket (with progress) and loads 1,000,000 rows on the machine. Loading the workflow again doesn't upload the file again.
- [ ] **Cloud Test & Score:** 5-fold cross-validation of Random Forest and Logistic Regression on all rows. AUC about 0.83 and 0.77. The status line says how long it took and what it cost ("Cost about $… at $0.15/h.").
- [ ] **ROC Analysis** (Orange's own widget) draws both curves.
- [ ] **Cloud Learner** trains Logistic Regression on the machine, and **Cloud Predictions** predicts all rows.
- [ ] **Cloud Data Table** shows 1,000,000 rows with the predictions; paging works.
- [ ] No widget shows an error. "Cost so far" in the dialog has grown, with the jobs' share in brackets.

## 4. Idle shutdown

Leave the workflow alone. Don't click anything in the Cloud widgets.

- [ ] Within about two minutes after the idle timeout has passed since the last job, the dialog says "The machine stopped: idle for longer than the idle timeout." and the widgets say they aren't connected.
- [ ] The three `gcloud` commands above list nothing: the add-on deleted the stopped VM, its disk and the firewall rule.

If Orange was closed when the machine stopped, the VM is stopped but not deleted. Opening Orange again finds it stopped and deletes it; "Delete them…" in the dialog does the same for any machine left behind.

## 5. Stop

1. Start the machine again and wait until Cloud File has loaded.
2. Close Orange and open it again with the workflow.
   - [ ] It reconnects to the same machine, and the widgets work without starting a new one.
3. Click **Stop**.
   - [ ] The dialog says the machine is stopped within about a minute, and records its cost.
   - [ ] The three `gcloud` commands list nothing.

## 6. Clean up

- [ ] In Cloud File, "Uploaded files…" lists the demo file; delete it. The bucket stays, empty, for next time (an empty bucket costs nothing).
- [ ] The three `gcloud` commands list nothing.

## What to expect

Measured on a Small machine in `us-central1`, with the demo's 1,000,000 rows:

| Step | Time |
|---|---|
| Start to connected | 75–90 s: creating the firewall rule and the VM ~20 s, booting ~20 s, downloading the machine image ~13 s, installing Python and the packages ~16 s |
| Cloud File (upload and load) | about 15 s, plus the upload on your connection |
| Cloud Test & Score | about 1 min 40 s (both learners, 5 folds, all rows) |
| Cloud Learner and Cloud Predictions | about 2 s and 7 s |
| Whole workflow after connecting | about 2.5 min |
| Idle shutdown noticed | 1.5–2.5 min after the idle timeout |
| Stop | about 1 min |
| Cost of one pass | about $0.02 |
